#!/usr/bin/env python3
"""ffn-bcm -- drive the BCM88375 Qumran-MX from the control plane.

Uses the CP<->DP transport (tools/ffn_cpdp.py) to reach the Qumran's CMIC window
at BAR2. No Broadcom BDE and no kernel module: the CMIC is the documented CPU
interface, and S-channel is all the vendor SDK uses underneath for register and
table access.

Every bit position here was recovered from the BCM88375_A0 field database inside
bcm.user.dbg -- CMIC_COMMON_SCHAN_CTRL {MSG_START:0, MSG_DONE:1, ABORT:2,
SER_CHECK_FAIL:20, NACK:21, TIMEOUT:22, SCHAN_ERROR:23} and CMIC_LEDUP0_CTRL
{LEDUP_EN:0}. None of it is guessed.

usage:
  ffn_bcm.py rd <bar2-offset> [--count N]
  ffn_bcm.py wr <bar2-offset> <value>
  ffn_bcm.py ledstat [<unit>]
  ffn_bcm.py ledload <file> [<unit>]
  ffn_bcm.py leden [<unit>] [--off]
  ffn_bcm.py schan <word> [<word> ...] [--recv N]
"""
import argparse
import struct
import sys

sys.path.insert(0, "/opt/ffn-ngfw-v2")
sys.path.insert(0, "/opt/ffn-ngfw-v2/tools")
from ffn_cpdp import Transport, ST

OP_BCM_RD, OP_BCM_WR = 10, 11
OP_SCHAN = 12
OP_LED_LOAD, OP_LED_ENABLE = 13, 14
OP_LED_GET = 9

LEDUP0_CTRL = 0x20000
LEDUP0_CLK_DIV = 0x2005C
LEDUP0_DATA_RAM = 0x20400
LEDUP0_PROG_RAM = 0x20800
LEDUP_STRIDE = 0x1000
LEDUP_EN = 1 << 0
RAMSZ = 256

SCHAN_BITS = [(0, "MSG_START"), (1, "MSG_DONE"), (2, "ABORT"),
              (20, "SER_CHECK_FAIL"), (21, "NACK"), (22, "TIMEOUT"),
              (23, "SCHAN_ERROR")]


def decode_schan_ctrl(v):
    on = [n for b, n in SCHAN_BITS if v & (1 << b)]
    return ", ".join(on) if on else "idle"


def ok(st, what):
    if st != 0:
        print("  %s failed: %s" % (what, ST.get(st, st)))
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("rd")
    p.add_argument("off")
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("wr")
    p.add_argument("off")
    p.add_argument("value")
    p = sub.add_parser("ledstat")
    p.add_argument("unit", nargs="?", type=int, default=0)
    p = sub.add_parser("ledload")
    p.add_argument("file")
    p.add_argument("unit", nargs="?", type=int, default=0)
    p.add_argument("--verify", action="store_true")
    p = sub.add_parser("leden")
    p.add_argument("unit", nargs="?", type=int, default=0)
    p.add_argument("--off", action="store_true")
    p = sub.add_parser("schan")
    p.add_argument("words", nargs="+")
    p.add_argument("--recv", type=int, default=4)
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return 2

    with Transport() as t:
        if not t.wait_ready():
            print("DP transport not ready -- is /sbin/ffn_cpdpd running?")
            return 2

        if a.cmd == "rd":
            off = int(a.off, 0)
            st, _, _, body = t.call(OP_BCM_RD, off, a.count)
            if not ok(st, "rd"):
                return 1
            for i in range(len(body) // 4):
                v = struct.unpack(">I", body[i * 4:i * 4 + 4])[0]
                print("  BAR2+0x%05x = 0x%08x" % (off + i * 4, v))

        elif a.cmd == "wr":
            st, _, _, _ = t.call(OP_BCM_WR, int(a.off, 0), int(a.value, 0))
            if not ok(st, "wr"):
                return 1
            print("  wrote 0x%08x -> BAR2+0x%05x"
                  % (int(a.value, 0), int(a.off, 0)))

        elif a.cmd == "ledstat":
            u = a.unit
            st, ctrl, div, _ = t.call(OP_LED_GET, u)
            if not ok(st, "ledstat"):
                return 1
            print("  LEDUP%d CTRL    = 0x%08x   LEDUP_EN=%s"
                  % (u, ctrl, "ON" if ctrl & LEDUP_EN else "off"))
            print("  LEDUP0 CLK_DIV = 0x%08x   (%d)" % (div, div))
            st, _, _, body = t.call(OP_BCM_RD,
                                    LEDUP0_PROG_RAM + u * LEDUP_STRIDE, 8)
            if st == 0:
                pr = [struct.unpack(">I", body[i * 4:i * 4 + 4])[0] & 0xFF
                      for i in range(len(body) // 4)]
                print("  PROGRAM_RAM[0:8] = %s"
                      % " ".join("%02x" % b for b in pr))
            st, _, _, body = t.call(OP_BCM_RD,
                                    LEDUP0_DATA_RAM + u * LEDUP_STRIDE, 8)
            if st == 0:
                dr = [struct.unpack(">I", body[i * 4:i * 4 + 4])[0] & 0xFF
                      for i in range(len(body) // 4)]
                print("  DATA_RAM[0:8]    = %s"
                      % " ".join("%02x" % b for b in dr))

        elif a.cmd == "ledload":
            prog = open(a.file, "rb").read()
            if len(prog) > RAMSZ:
                print("  program is %d bytes, PROGRAM_RAM holds %d"
                      % (len(prog), RAMSZ))
                return 1
            st, n, _, _ = t.call(OP_LED_LOAD, a.unit, payload=prog)
            if not ok(st, "ledload"):
                return 1
            print("  loaded %d program bytes into LEDUP%d PROGRAM_RAM"
                  % (n, a.unit))
            # read the first bytes back so a silent failure cannot pass
            st, _, _, body = t.call(OP_BCM_RD,
                                    LEDUP0_PROG_RAM + a.unit * LEDUP_STRIDE,
                                    min(16, len(prog)))
            if st == 0:
                got = bytes(struct.unpack(">I", body[i * 4:i * 4 + 4])[0] & 0xFF
                            for i in range(len(body) // 4))
                same = got == prog[:len(got)]
                print("  readback[0:%d] %s" % (len(got),
                                               "MATCH" if same else "MISMATCH"))
                if not same:
                    print("    wrote %s" % prog[:len(got)].hex())
                    print("    read  %s" % got.hex())
                    return 1

        elif a.cmd == "leden":
            on = 0 if a.off else 1
            st, ctrl, _, _ = t.call(OP_LED_ENABLE, a.unit, on)
            if not ok(st, "leden"):
                return 1
            print("  LEDUP%d CTRL = 0x%08x   LEDUP_EN=%s"
                  % (a.unit, ctrl, "ON" if ctrl & LEDUP_EN else "off"))
            if on and not (ctrl & LEDUP_EN):
                print("  NOTE: the enable bit did not stick -- the LED "
                      "processor may be gated elsewhere")

        elif a.cmd == "schan":
            words = [int(w, 0) for w in a.words]
            pay = b"".join(struct.pack(">I", w & 0xFFFFFFFF) for w in words)
            st, ctrl, _, body = t.call(OP_SCHAN, len(words), a.recv,
                                       payload=pay)
            print("  SCHAN_CTRL = 0x%08x  (%s)" % (ctrl, decode_schan_ctrl(ctrl)))
            for i in range(len(body) // 4):
                v = struct.unpack(">I", body[i * 4:i * 4 + 4])[0]
                print("  MESSAGE%-2d = 0x%08x" % (i, v))
            if not ok(st, "schan"):
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
