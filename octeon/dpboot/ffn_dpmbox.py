#!/usr/bin/env python
"""ffn-dpmbox -- read the DP bootloader mailbox from the CP, safely.

Runs ON the CP (MIPS64 big-endian, vendor Python 2.7) against the DP endpoint
0003:03:00.0, which sits downstream of the CP's PCIe behind two PLX PEX8606
bridges.

WHY THIS EXISTS: the vendor MIPS tools destabilise FFN's CP kernel. oct-remote-dump
leaks preempt_count ("exited with preempt_count 1") and oct-remote-reset has
returned SIGBUS; both have ended in an NMI watchdog and "*** Chip soft reset
soon ***", taking the whole CP down. A plain mmap of the endpoint's sysfs BAR has
never crashed anything, so all *reads* go through this instead.

TWO PREREQUISITES, both learned the hard way:
  1. Memory-Space-Enable. Nothing binds a driver to the DP, so Linux never runs
     pci_enable_bridge() and BOTH PLX bridges sit at COMMAND=0x0000 -- they
     forward no memory traffic and every read returns all-ones (which is NOT a
     register value; it is an unsupported request). `echo 1 > .../enable` calls
     pci_enable_device(), which walks UP the chain and fixes the bridges.
     Re-assert after every reset.
  2. The BAR1 window. resource2 is a windowed BAR: its index registers must point
     at DRAM before reads resolve, exactly as on the CP (spem0_bar1_index1).
     Until programmed it also reads all-ones, even with MSE set.

The mailbox protocol is the one FFN already uses on the CP (ffn_octctl.py): a
big-endian state word at OCTEON DRAM 0x6C000, where 2 == READY == a bootloader is
polling for a command. Reading it answers the question oct-remote-bootcmd cannot:
that tool writes blind and returns rc=0 whether or not anything is listening.
"""
from __future__ import print_function

import mmap
import os
import struct
import sys

DP_PCI = "0003:03:00.0"
SYSFS = "/sys/bus/pci/devices"
MBOX_STATE = 0x6C000        # OCTEON DRAM offset of the bootloader mailbox
STATE_READY = 2
WINDOW_BAR = 2              # sysfs resource2 == OCTEON BAR1 (windowed)
MAP_LEN = 1 << 20


def enable(pci=DP_PCI, quiet=False):
    """pci_enable_device() on the endpoint -- also enables the parent bridges."""
    path = os.path.join(SYSFS, pci, "enable")
    try:
        fh = open(path, "w")
        try:
            fh.write("1")
        finally:
            fh.close()
        return True
    except Exception as exc:
        if not quiet:
            print("enable %s: %s" % (path, exc))
        return False


def bridge_commands(pci=DP_PCI):
    """COMMAND register of each device on the path, as PCI little-endian values.

    The CP is big-endian MIPS while PCI config space is little-endian, so the
    bytes must be swapped by hand -- reading these with `od -t x2` silently
    reports them byte-reversed.
    """
    out = []
    for dev in ("0003:01:00.0", "0003:02:01.0", pci):
        try:
            fh = open(os.path.join(SYSFS, dev, "config"), "rb")
            try:
                fh.seek(4)
                raw = bytearray(fh.read(2))
            finally:
                fh.close()
            val = raw[0] | (raw[1] << 8)        # little-endian, explicitly
            out.append((dev, val, bool(val & 0x2), bool(val & 0x4)))
        except Exception as exc:
            out.append((dev, None, False, False))
    return out


def read_window(offset, length=32, pci=DP_PCI):
    """Read from the DP's windowed BAR. Returns bytes, or None if unreadable."""
    path = os.path.join(SYSFS, pci, "resource%d" % WINDOW_BAR)
    try:
        fd = os.open(path, os.O_RDONLY)
    except Exception as exc:
        print("open %s: %s" % (path, exc))
        return None
    try:
        try:
            mm = mmap.mmap(fd, MAP_LEN, mmap.MAP_SHARED, mmap.PROT_READ)
        except Exception as exc:
            print("mmap %s: %s" % (path, exc))
            return None
        try:
            return bytes(bytearray(mm[offset:offset + length]))
        finally:
            mm.close()
    finally:
        os.close(fd)


def mailbox_state(pci=DP_PCI):
    """(state, note). state is None when the window is not answering."""
    raw = read_window(MBOX_STATE, 8, pci)
    if raw is None:
        return None, "BAR unreadable"
    data = bytearray(raw)
    if len(data) < 4:
        return None, "short read"
    if all(b == 0xFF for b in data[:4]):
        return None, ("all-ones: window not answering -- MSE clear on a bridge, "
                      "or BAR1 index not programmed, or the DP is in reset")
    return struct.unpack(">I", bytes(data[:4]))[0], None


def main(argv):
    pci = argv[1] if len(argv) > 1 else DP_PCI
    print("=== DP endpoint %s ===" % pci)
    enable(pci)

    print("--- PCIe path (memory reads die unless every MSE is set) ---")
    for dev, val, mse, bme in bridge_commands(pci):
        if val is None:
            print("  %-14s COMMAND unreadable" % dev)
        else:
            print("  %-14s COMMAND=0x%04x  MSE=%-5s BME=%s"
                  % (dev, val, mse, bme))

    print("--- bootloader mailbox @ DRAM 0x%X ---" % MBOX_STATE)
    state, note = mailbox_state(pci)
    if state is None:
        print("  unreadable: %s" % note)
        return 2
    raw = bytearray(read_window(MBOX_STATE, 16, pci) or b"")
    print("  raw: %s" % " ".join("%02x" % b for b in raw))
    if state == STATE_READY:
        print("  state=%d READY -- a bootloader IS polling; a bootcmd will be "
              "consumed" % state)
        return 0
    print("  state=%d NOT READY -- nothing is polling, so oct-remote-bootcmd's "
          "rc=0 means nothing" % state)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
