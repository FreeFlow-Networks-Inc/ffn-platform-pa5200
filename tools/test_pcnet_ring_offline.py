#!/usr/bin/env python3
"""Offline tests for ffn_pcnet_ring: no hardware, a bytearray stands in for DRAM.

Covers the properties that make the ring safe against a peer that writes
anything it likes into the shared region:

  * a bad slot (len 0, len out of range, CRC mismatch) is consumed, so the
    consumer moves on instead of retrying it forever -- the wedge;
  * head and tail are masked, so no value the peer writes can index outside
    the ring;
  * the ingress filter admits only the traffic the link exists to carry.

Run:  python3 tools/test_pcnet_ring_offline.py
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_pcnet_ring as pn


def fresh():
    region = bytearray(pn.SIZE)

    def rd(off, n):
        assert 0 <= off and off + n <= len(region), "read outside region"
        return bytes(region[off:off + n])

    def wr(off, data):
        assert 0 <= off and off + len(data) <= len(region), "write outside region"
        region[off:off + len(data)] = data

    pn.host_init(rd, wr)
    return region, rd, wr


def raw_u32(rd, off):
    return struct.unpack(">I", rd(off, 4))[0]


def set_u32(wr, off, v):
    wr(off, struct.pack(">I", v & 0xffffffff))


def test_roundtrip():
    _, rd, wr = fresh()
    r = pn.Ring(pn.O2H_OFF, rd, wr)
    frames = [bytes([i]) * (i + 1) for i in range(10)]
    for f in frames:
        assert r.put(f)
    assert [r.get() for _ in frames] == frames
    assert r.get() is None
    assert raw_u32(rd, pn.O2H_OFF) == raw_u32(rd, pn.O2H_OFF + 4) == 10


def test_full():
    _, rd, wr = fresh()
    r = pn.Ring(pn.O2H_OFF, rd, wr)
    for _ in range(pn.NSLOTS - 1):
        assert r.put(b"x")
    assert not r.put(b"x")                      # one slot always left unused
    assert raw_u32(rd, pn.O2H_OFF + 8) == 1     # producer_drops bumped


def test_bad_crc_is_consumed():
    """The wedge. A CRC mismatch used to raise before tail moved."""
    _, rd, wr = fresh()
    r = pn.Ring(pn.O2H_OFF, rd, wr)
    assert r.put(b"hello world")
    assert r.put(b"next")
    off = pn.O2H_OFF + pn.slot_off(0) + pn.SLOT_HDR
    wr(off, b"j")                                # corrupt one payload byte
    try:
        r.get()
        assert False, "expected RingError"
    except pn.RingError:
        pass
    assert raw_u32(rd, pn.O2H_OFF + 4) == 1      # tail advanced past the bad slot
    assert raw_u32(rd, pn.O2H_OFF + pn.slot_off(0)) == 0   # len cleared
    assert r.get() == b"next"                    # and the ring keeps flowing
    assert r.get() is None


def test_len_zero_is_consumed():
    _, rd, wr = fresh()
    r = pn.Ring(pn.O2H_OFF, rd, wr)
    assert r.put(b"a")
    assert r.put(b"b")
    set_u32(wr, pn.O2H_OFF + pn.slot_off(0), 0)  # len 0 inside [tail, head)
    try:
        r.get()
        assert False, "expected RingError"
    except pn.RingError:
        pass
    assert r.get() == b"b"
    assert r.get() is None


def test_len_out_of_range_is_consumed():
    _, rd, wr = fresh()
    r = pn.Ring(pn.O2H_OFF, rd, wr)
    assert r.put(b"a")
    assert r.put(b"b")
    set_u32(wr, pn.O2H_OFF + pn.slot_off(0), 0x7fffffff)
    try:
        r.get()
        assert False, "expected RingError"
    except pn.RingError:
        pass
    assert r.get() == b"b"


def test_indices_are_masked():
    assert pn.slot_off(0xffffffff) == pn.slot_off(pn.NSLOTS - 1)
    assert pn.slot_off(pn.NSLOTS) == pn.slot_off(0)

    _, rd, wr = fresh()
    r = pn.Ring(pn.O2H_OFF, rd, wr)
    # Peer writes garbage indices. Masked: head 0x100 -> 0, tail 0xffffff01 -> 1.
    set_u32(wr, pn.O2H_OFF + 0, 0x100)
    set_u32(wr, pn.O2H_OFF + 4, 0xffffff01)
    # Slot 1 is empty (len 0) so this is a consumed bad slot, and every access
    # stayed inside the region (rd/wr assert otherwise).
    try:
        r.get()
        assert False, "expected RingError"
    except pn.RingError:
        pass
    assert raw_u32(rd, pn.O2H_OFF + 4) == 2      # written back masked
    # Producer side: a garbage head still lands in a real slot.
    set_u32(wr, pn.O2H_OFF + 0, 0xdeadbeef)      # masks to 0xef = 239
    set_u32(wr, pn.O2H_OFF + 4, 239)             # empty at that index
    assert r.put(b"z")
    assert raw_u32(rd, pn.O2H_OFF + 0) == 240
    assert r.get() == b"z"


def eth(dst_type, payload):
    return b"\x02" * 6 + b"\x02" * 6 + struct.pack(">H", dst_type) + payload


def ipv4(src, dst, ihl=5):
    hdr = bytes([0x40 | ihl, 0]) + struct.pack(">HHHBBH", 40, 0, 0, 64, 1, 0)
    hdr += struct.pack(">II", pn.ip4(src), pn.ip4(dst)) + b"\x00" * (ihl * 4 - 20)
    return eth(pn.ETH_IPV4, hdr + b"\x00" * 8)


def arp(spa, tpa):
    body = struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1)
    body += b"\x02" * 6 + struct.pack(">I", pn.ip4(spa))
    body += b"\x00" * 6 + struct.pack(">I", pn.ip4(tpa))
    return eth(pn.ETH_ARP, body)


def test_ingress_filter():
    local = pn.ip4("127.1.1.1")
    allowed = [pn.cidr("127.1.1.2/32"), pn.cidr("127.1.2.0/24")]
    ok = lambda f: pn.ingress_ok(f, local, allowed)

    assert ok(ipv4("127.1.1.2", "127.1.1.1"))       # the CP, to us
    assert ok(ipv4("127.1.2.2", "127.1.1.1"))       # the DP via the CP, to us
    assert ok(arp("127.1.1.2", "127.1.1.1"))        # CP ARPs for us
    assert not ok(ipv4("127.1.1.2", "127.0.0.1"))   # loopback services: no
    assert not ok(ipv4("127.1.1.2", "127.1.1.255")) # broadcast: no
    assert not ok(ipv4("127.1.1.2", "10.0.0.1"))    # anything else: no
    assert not ok(ipv4("127.1.3.9", "127.1.1.1"))   # unknown source: no
    assert not ok(ipv4("127.0.0.1", "127.1.1.1"))   # spoofed loopback: no
    assert not ok(arp("127.1.1.2", "127.1.1.9"))    # ARP for someone else: no
    assert not ok(arp("127.1.9.9", "127.1.1.1"))    # ARP from a stranger: no
    assert not ok(eth(0x86dd, b"\x60" + b"\x00" * 39))   # IPv6: no
    assert not ok(eth(0x8100, b"\x00" * 60))        # VLAN-tagged: no
    assert not ok(ipv4("127.1.1.2", "127.1.1.1")[:30])   # truncated: no
    assert not ok(b"\x00" * 13)                     # runt: no
    assert not ok(ipv4("127.1.1.2", "127.1.1.1", ihl=4))  # bad IHL: no
    # options-bearing IPv4 header still parses (ihl 6)
    assert ok(ipv4("127.1.1.2", "127.1.1.1", ihl=6))


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  %s" % t.__name__)
    print("%d tests passed" % len(tests))


if __name__ == "__main__":
    main()
