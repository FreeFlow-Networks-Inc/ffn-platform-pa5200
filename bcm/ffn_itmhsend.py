#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Send a frame prefixed with an Ingress TM header.

The chip parses the first four bytes of every frame arriving on a port whose
`tm_port_header_type_in` is TM, and takes the forwarding destination from them.

Layout, from the vendor's own DWARF (struct dune_itmh_v3_s, 4 bytes, in
libpandp_cp.so):

    [31:30] type   [29] mirr_dis  [28:27] dp   [26:8] dst_prt
    [7:4]  snoop   [3:1] tclass   [0]  ext

## The destination is a tagged field, which is why the first sweep failed

`bcm/ITMH.md` records sweeping dst_prt 0..1023 across all four type values --
about 1300 values -- every one rejected with IqmRjctQnvalidErrPktCnt. The
vendor's own `usr/share/broadcom/dsa_tag_support.c` documents why. dst_prt is
an L2 Destination, and an L2 Destination carries a 3-bit tag:

    |8|7|6|5|4|3|2|1|0|9|8|7|6|5|4|3|2|1|0|
    |0|0|1|      System-Port-Agr          |
    |0|0|1|0|      System-Port            |
    |0|0|1|0|0|0|0|0|0|0|0|0|0| Dest-Port |

Bits [18:16] must be 0b001. A bare port number leaves them 0b000, so the whole
of that sweep sat outside the encoding's valid space and could not have hit.

    dst_prt = 0x10000 | port        ->  itmh = dst_prt << 8

THIS IS NOT NEW, and the encoding below is not a proposal. `COPPER-FORWARDING.md`
records the format already validated on hardware on 2026-09-16:

    "four bytes `01 <BCM destination BE16> 00`"

which is the same word. What this file adds is only that the encoding is now
built into the tool with the tagged field named, instead of every caller
hand-assembling a header and the 0..1023 form remaining the easy mistake to
make. The bounded copper test also shows why the header alone was never the
whole story: BCM15 had no VOQ bundle at all, and "a synchronized PHY/MAC and an
enabled port cannot substitute for queue resources, scheduler connections and an
ingress destination".

## Why this refuses to run by default

The previous version of this file asserted in a comment that "port 5 is
tm_port_header_type_in=TM". That stopped being true when ports 5, 8 and 24 were
moved to ETH for L2 bridging, and worse, that change is hand-applied and does
not survive a switch re-init -- so the class of a given port depends on whether
anyone re-applied it since the last init. Injecting into an ETH-classed port
produces a confusing non-result rather than an error.

So the ingress port's class is checked against a captured chip config before
anything is sent, and a mismatch is fatal unless --force is given. A stale
assumption should fail loudly, not quietly waste an experiment.
"""
import argparse
import glob
import json
import os
import re
import socket
import struct
import sys

SYSPORT_AGR = 0x10000            # L2 Destination bits[18:16] = 0b001


def itmh(port=0, *, typ=0, mirr_dis=0, dp=0, snoop=0, tclass=0, ext=0, raw_dst=None):
    """Build the 32-bit ingress TM header."""
    dst = raw_dst if raw_dst is not None else (SYSPORT_AGR | port)
    if not 0 <= dst < (1 << 19):
        raise ValueError("dst_prt 0x%x does not fit in 19 bits" % dst)
    return ((typ & 3) << 30 | (mirr_dis & 1) << 29 | (dp & 3) << 27 |
            (dst & 0x7FFFF) << 8 | (snoop & 0xF) << 4 |
            (tclass & 7) << 1 | (ext & 1))


def newest_config():
    here = os.path.dirname(os.path.abspath(__file__))
    found = sorted(glob.glob(os.path.join(here, "BCM-CONFIG-*.json")))
    return found[-1] if found else None


def header_type(config_path, port, direction="in"):
    """Return tm_port_header_type_<direction>_<port> from a captured config."""
    with open(config_path) as fh:
        text = json.dumps(json.load(fh))
    m = re.search(r"tm_port_header_type_%s_%d\.BCM88650=([A-Z_]+)"
                  % (direction, port), text)
    return m.group(1) if m else None


def build(iface, word, tag):
    with open("/sys/class/net/%s/address" % iface) as fh:
        srcb = bytes(int(x, 16) for x in fh.read().strip().split(":"))
    payload = ("FFN-ITMH-" + tag).encode()[:46].ljust(46, b"\0")
    return (struct.pack("!I", word) + b"\xff" * 6 + srcb +
            struct.pack("!H", 0x0800) + payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iface", required=True, help="netdev to inject on")
    ap.add_argument("--ingress-port", type=int, required=True,
                    help="BCM port the frame enters; its class is verified")
    dest = ap.add_mutually_exclusive_group(required=True)
    dest.add_argument("--dest-port", type=int, help="encoded as 0x10000|port")
    dest.add_argument("--sweep", help="inclusive port range, e.g. 0-40")
    dest.add_argument("--raw", help="literal 19-bit dst_prt, e.g. 0x1000d")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--tclass", type=int, default=0)
    ap.add_argument("--config", help="captured chip config "
                                     "(default: newest bcm/BCM-CONFIG-*.json)")
    ap.add_argument("--force", action="store_true",
                    help="inject even if the ingress port is not TM-classed")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the headers and send nothing")
    a = ap.parse_args()

    cfg = a.config or newest_config()
    if cfg:
        cls = header_type(cfg, a.ingress_port)
        print("ingress port %d: tm_port_header_type_in=%s  (per %s)"
              % (a.ingress_port, cls, os.path.basename(cfg)))
        if cls != "TM" and not a.force:
            return ("port %d is %s, not TM -- the chip will not read an ITMH "
                    "there, so any result would be meaningless. Re-apply the "
                    "fabric config, pick a TM port, or pass --force."
                    % (a.ingress_port, cls))
    elif not a.force:
        return ("no BCM-CONFIG-*.json to verify the ingress port class against; "
                "pass --config or --force")

    if a.sweep:
        lo, hi = (int(x, 0) for x in a.sweep.split("-"))
        targets = [(p, itmh(p, tclass=a.tclass)) for p in range(lo, hi + 1)]
    elif a.raw is not None:
        targets = [(None, itmh(tclass=a.tclass, raw_dst=int(a.raw, 0)))]
    else:
        targets = [(a.dest_port, itmh(a.dest_port, tclass=a.tclass))]

    if a.dry_run:
        for p, w in targets:
            print("dest=%-4s dst_prt=0x%05x itmh=0x%08x"
                  % (p, (w >> 8) & 0x7FFFF, w))
        return 0

    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
    sock.bind((a.iface, 0))
    for p, w in targets:
        tag = "P%s" % p if p is not None else "RAW"
        frame = build(a.iface, w, tag)
        for _ in range(a.count):
            sock.send(frame)
        print("sent %d  dest=%-4s dst_prt=0x%05x itmh=0x%08x  tag=%s"
              % (a.count, p, (w >> 8) & 0x7FFFF, w, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
