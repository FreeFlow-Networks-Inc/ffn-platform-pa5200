#!/usr/bin/env python3
"""Mark one conntrack entry TCP 'be liberal' in both directions (what the kernel flowtable does
when a flow comes back from hardware), through nfnetlink. Run inside the data namespace:

  ip netns exec ffn-data python3 ct_liberal.py SRC SPORT DST DPORT [--proto 6] [--clear]
"""
import argparse
import os
import socket
import struct
import sys

NETLINK_NETFILTER = 12
NFNL_SUBSYS_CTNETLINK = 1
IPCTNL_MSG_CT_NEW = 0
NLM_F_REQUEST, NLM_F_ACK = 1, 4
NLA_F_NESTED = 0x8000
CTA_TUPLE_ORIG, CTA_PROTOINFO, CTA_TIMEOUT = 1, 4, 7
CTA_TUPLE_IP, CTA_TUPLE_PROTO = 1, 2
CTA_IP_V4_SRC, CTA_IP_V4_DST = 1, 2
CTA_PROTO_NUM, CTA_PROTO_SRC_PORT, CTA_PROTO_DST_PORT = 1, 2, 3
CTA_PROTOINFO_TCP = 1
CTA_PROTOINFO_TCP_FLAGS_ORIGINAL, CTA_PROTOINFO_TCP_FLAGS_REPLY = 4, 5
IP_CT_TCP_FLAG_BE_LIBERAL = 0x08


def attr(kind, payload):
    pad = (-len(payload)) % 4
    return struct.pack('=HH', 4 + len(payload), kind) + payload + bytes(pad)


def nested(kind, *children):
    return attr(kind | NLA_F_NESTED, b''.join(children))


def message(src, sport, dst, dport, proto, flags, mask, seq, timeout=None):
    tuple_orig = nested(CTA_TUPLE_ORIG,
                        nested(CTA_TUPLE_IP, attr(CTA_IP_V4_SRC, socket.inet_aton(src)), attr(CTA_IP_V4_DST, socket.inet_aton(dst))),
                        nested(CTA_TUPLE_PROTO, attr(CTA_PROTO_NUM, bytes([proto])),
                               attr(CTA_PROTO_SRC_PORT, struct.pack('!H', sport)), attr(CTA_PROTO_DST_PORT, struct.pack('!H', dport))))
    tcp = nested(CTA_PROTOINFO, nested(CTA_PROTOINFO_TCP,
                                       attr(CTA_PROTOINFO_TCP_FLAGS_ORIGINAL, bytes([flags, mask])),
                                       attr(CTA_PROTOINFO_TCP_FLAGS_REPLY, bytes([flags, mask]))))
    body = struct.pack('=BBH', socket.AF_INET, 0, 0) + tuple_orig + tcp
    if timeout is not None:
        body += attr(CTA_TIMEOUT, struct.pack('!I', timeout))
    kind = (NFNL_SUBSYS_CTNETLINK << 8) | IPCTNL_MSG_CT_NEW
    return struct.pack('=IHHII', 16 + len(body), kind, NLM_F_REQUEST | NLM_F_ACK, seq, 0) + body


def main():
    p = argparse.ArgumentParser()
    p.add_argument('src'); p.add_argument('sport', type=int); p.add_argument('dst'); p.add_argument('dport', type=int)
    p.add_argument('--proto', type=int, default=6)
    p.add_argument('--clear', action='store_true', help='clear the flag instead of setting it')
    p.add_argument('--timeout', type=int, help='also reset the entry timeout to this many seconds')
    a = p.parse_args()
    flags = 0 if a.clear else IP_CT_TCP_FLAG_BE_LIBERAL
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER)
    s.bind((0, 0))
    seq = os.getpid() & 0xffffff
    s.send(message(a.src, a.sport, a.dst, a.dport, a.proto, flags, IP_CT_TCP_FLAG_BE_LIBERAL, seq, a.timeout))
    reply = s.recv(65536)
    length, kind, nflags, rseq, pid = struct.unpack_from('=IHHII', reply, 0)
    if kind != 2:  # NLMSG_ERROR carries the ack
        print('unexpected netlink reply type %d' % kind, file=sys.stderr); return 2
    error = struct.unpack_from('=i', reply, 16)[0]
    if error:
        print('conntrack update failed: %s' % os.strerror(-error), file=sys.stderr); return 1
    print('%s %s:%d -> %s:%d proto %d be_liberal=%s timeout=%s' % ('cleared' if a.clear else 'set', a.src, a.sport, a.dst, a.dport, a.proto, not a.clear, a.timeout))
    return 0


if __name__ == '__main__':
    sys.exit(main())
