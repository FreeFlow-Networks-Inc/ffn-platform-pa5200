#!/usr/bin/env python
"""ffn-dpsend -- hand a u-boot command to the DP bootloader, from the CP.

WHY: oct-remote-bootcmd does not work against the DP from the big-endian CP. It
writes the command STRING into the mailbox and then returns rc=0, but leaves
LEN=0 and STATE=2 (READY) -- it never publishes the command-present flag, so the
DP's u-boot correctly concludes there is nothing to do. Verified by reading the
mailbox back: the string was there, the flag was not. (The tool also reports
"Remote host is LE" while running on a BE MIPS CP, so its byte handling is not
trustworthy here either.)

This implements the vendor mailbox protocol directly -- the same one FFN already
uses on the CP (ffn_oct.oct_send_bootcmd), recovered from the vendor loader:

    0x6c000  u32 STATE  2 = bootloader ready, 1 = command present
    0x6c004  u32 LEN    length of the command
    0x6c008      CMD    the command string (<= 247 bytes)

Ordering is the safety property: string, then length, then the flag LAST, so the
bootloader can never act on a half-written command. The state word returning to
READY is the acknowledgement that it consumed it.

BYTE ORDER: reads/writes through the DP's BAR1 window arrive byte-reversed within
each aligned 64-bit word (proved empirically: the queued "bootoctl" reads back as
"ltcotoob"). So every access here is staged in the DP's own view and then reversed
per 8-byte group on the way out. STATE and LEN share one 64-bit group, so they are
written as a single group -- which is fine, because the string is already in place
by then.

PREREQUISITES: the PLX bridges need Memory-Space-Enable (toggle the endpoint's
sysfs "enable" 0 -> 1; writing 1 when it is already 1 is a no-op that does NOT
re-walk the bridges), and PEM0_BAR1_INDEX0 must be 0x1 so BAR offset == DRAM
address.
"""
from __future__ import print_function

import argparse
import mmap
import os
import struct
import sys
import time

DP_PCI = "0003:03:00.0"
SYSFS = "/sys/bus/pci/devices"
WINDOW_BAR = 2

MBOX_STATE = 0x6C000
MBOX_LEN = 0x6C004
MBOX_CMD = 0x6C008
MBOX_MAXLEN = 0xF7              # 247; the vendor loader refuses longer
STATE_READY = 2
STATE_CMD_PRESENT = 1
POLL_SEC = 0.5
MAP_LEN = 1 << 20


def reenable(pci):
    """Force a real pci_enable_device() so the parent PLX bridges get MSE."""
    path = os.path.join(SYSFS, pci, "enable")
    for val in ("0", "1"):
        try:
            fh = open(path, "w")
            try:
                fh.write(val)
            finally:
                fh.close()
        except Exception:
            pass
        time.sleep(0.3)


def swap8(data):
    """Reverse each aligned 8-byte group (the DP window swaps 64-bit words)."""
    src = bytearray(data)
    out = bytearray(len(src))
    n = (len(src) // 8) * 8
    for i in range(0, n, 8):
        out[i:i + 8] = src[i:i + 8][::-1]
    if n < len(src):
        out[n:] = src[n:]
    return bytes(out)


class Window(object):
    """Read/write the DP's windowed BAR, in the DP's own byte order."""

    def __init__(self, pci):
        self.path = os.path.join(SYSFS, pci, "resource%d" % WINDOW_BAR)
        self.fd = os.open(self.path, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, MAP_LEN, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)

    def read_group(self, off):
        """Read one aligned 8-byte group, in the DP's view."""
        base = off & ~7
        return swap8(bytes(bytearray(self.mm[base:base + 8])))

    def write_group(self, off, eight):
        """Write one aligned 8-byte group given in the DP's view."""
        base = off & ~7
        if len(eight) != 8:
            raise ValueError("group must be 8 bytes")
        self.mm[base:base + 8] = swap8(eight)

    def read_state(self):
        grp = self.read_group(MBOX_STATE)          # STATE|LEN share this group
        return struct.unpack(">I", grp[0:4])[0], struct.unpack(">I", grp[4:8])[0]

    def write_bytes_dpview(self, off, raw):
        """Write raw bytes at a DP address, padding to whole 8-byte groups."""
        start = off & ~7
        lead = off - start
        total = lead + len(raw)
        span = ((total + 7) // 8) * 8
        cur = bytearray()
        for i in range(0, span, 8):
            cur += bytearray(self.read_group(start + i))
        cur[lead:lead + len(raw)] = bytearray(raw)
        for i in range(0, span, 8):
            self.write_group(start + i, bytes(cur[i:i + 8]))


def main():
    ap = argparse.ArgumentParser(description="send a u-boot command to the DP")
    ap.add_argument("command", help="the u-boot command")
    ap.add_argument("--pci", default=DP_PCI)
    ap.add_argument("--wait", type=float, default=30.0,
                    help="seconds to wait for the bootloader to consume it")
    ap.add_argument("--force-ready", action="store_true",
                    help="send even if STATE is not READY")
    args = ap.parse_args()

    raw = args.command.encode()
    if len(raw) > MBOX_MAXLEN:
        print("refusing: command is %d bytes, the bootloader accepts at most %d"
              % (len(raw), MBOX_MAXLEN))
        return 2

    reenable(args.pci)
    try:
        win = Window(args.pci)
    except Exception as exc:
        print("cannot map the DP window: %s" % exc)
        return 2

    try:
        state, length = win.read_state()
        if state == 0xFFFFFFFF:
            print("window not answering (all-ones) -- MSE clear, or "
                  "PEM0_BAR1_INDEX0 not 0x1")
            return 2
        print("mailbox before: STATE=%d LEN=%d" % (state, length))
        if state != STATE_READY and not args.force_ready:
            print("bootloader not READY (STATE=%d); use --force-ready to send "
                  "anyway" % state)
            return 1

        # 1. the string, 2. the length, 3. the flag LAST (same group as length)
        win.write_bytes_dpview(MBOX_CMD, raw + b"\0")
        grp = struct.pack(">I", STATE_CMD_PRESENT) + struct.pack(">I", len(raw))
        win.write_group(MBOX_STATE, grp)
        st2, ln2 = win.read_state()
        print("mailbox armed : STATE=%d LEN=%d  (cmd %d bytes)"
              % (st2, ln2, len(raw)))

        # 4. STATE returning to READY is the acknowledgement
        deadline = time.time() + args.wait
        polls = 0
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            polls += 1
            st, ln = win.read_state()
            if st == STATE_READY:
                print("CONSUMED after %d poll(s) -- the bootloader took the "
                      "command" % polls)
                return 0
            if st == 0xFFFFFFFF:
                print("window stopped answering after %d poll(s) -- this is "
                      "EXPECTED if the command booted a kernel (it reprograms "
                      "the PCIe window)" % polls)
                return 0
        st, ln = win.read_state()
        print("NOT consumed within %.0fs (STATE=%d LEN=%d)" % (args.wait, st, ln))
        return 1
    finally:
        win.close()


if __name__ == "__main__":
    sys.exit(main())
