#!/usr/bin/env python
"""ffn-dpsh -- shell on the DP Octeon, over the CP's PCIe window.

The DP's console is its own ttyS0 and nothing on the CP can read it, so this drives
the shared-memory mailbox that ffn_dpagent serves from inside the DP. Runs ON the
CP (vendor python 2.7 in the NFS root); the DP end is /sbin/ffn_dpagent.

    ffn-dpsh -c "uname -a; nproc"      one command
    ffn-dpsh                            interactive
    ffn-dpsh --status                   is the agent alive?

MAILBOX (DP phys 0x00400000, 64 KB; all fields big-endian, DP-native):
    +0x0000  magic "FFNDPSH1"       +0x0018  u32 rsp_seq  (agent mirrors cmd_seq)
    +0x0008  u32 version            +0x001c  u32 rsp_len
    +0x000c  u32 agent_up           +0x0020  u32 rsp_status
    +0x0010  u32 cmd_seq (we bump)  +0x0100  command text
    +0x0014  u32 cmd_len            +0x1000  response text
Write order is payload -> length -> sequence, both directions, so neither side can
observe a half-written mailbox.

BYTE ORDER: accesses through this window land byte-reversed within each aligned
64-bit word (proven when a queued "bootoctl" read back as "ltcotoob"). All swapping
happens HERE so the DP side stays plain big-endian.

WINDOW: DP phys 0x400000 sits in BAR1 window index 1 (each index covers 4 MB), so
PEM0_BAR1_INDEX1 must hold ADDR_IDX 1 (value 0x11); BAR offset 0x400000 is then DP
phys 0x400000. The sysfs "enable" must be toggled 0 -> 1: writing 1 when it is
already 1 is a no-op that does NOT re-walk the PLX bridges, and reads come back
all-ones.
"""
from __future__ import print_function

import argparse
import mmap
import os
import struct
import subprocess
import sys
import time

PCI = "0003:03:00.0"
SYSFS = "/sys/bus/pci/devices"
CSR = "/usr/local/cp/oct-remote-csr"
RING_PHYS = 0x00400000
RING_SIZE = 0x10000

OFF_MAGIC, OFF_VER, OFF_UP = 0x0000, 0x0008, 0x000C
OFF_CMD_SEQ, OFF_CMD_LEN = 0x0010, 0x0014
OFF_RSP_SEQ, OFF_RSP_LEN, OFF_RSP_STAT = 0x0018, 0x001C, 0x0020
OFF_CMD, OFF_RSP = 0x0100, 0x1000
CMD_MAX, RSP_MAX = 0x0E00, 0xF000
MAGIC = b"FFNDPSH1"


def swap8(data):
    """Reverse each aligned 8-byte group -- the window's 64-bit byte swap."""
    src = bytearray(data)
    out = bytearray(len(src))
    n = (len(src) // 8) * 8
    for i in range(0, n, 8):
        out[i:i + 8] = src[i:i + 8][::-1]
    if n < len(src):
        out[n:] = src[n:]
    return bytes(out)


class Ring(object):
    def __init__(self, program_window=True):
        if program_window:
            self._enable()
            self._window()
        path = os.path.join(SYSFS, PCI, "resource2")
        self.fd = os.open(path, os.O_RDWR)
        span = 0x400000 + RING_SIZE          # window index 1 begins at 4 MB
        self.mm = mmap.mmap(self.fd, span, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)
        self.base = 0x400000                 # BAR offset == DP phys, per above

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)

    @staticmethod
    def _enable():
        p = os.path.join(SYSFS, PCI, "enable")
        for v in ("0", "1"):
            try:
                f = open(p, "w")
                try:
                    f.write(v)
                finally:
                    f.close()
            except Exception:
                pass
            time.sleep(0.3)

    @staticmethod
    def _window():
        env = dict(os.environ)
        env["OCTEON_PCI_IDS"] = "0x177d0095"
        env["LD_LIBRARY_PATH"] = "/usr/local/lib64"
        null = open(os.devnull, "w")
        try:
            subprocess.call([CSR, "--devnum=1", "PEM0_BAR1_INDEX1", "0x11"],
                            env=env, stdout=null, stderr=subprocess.STDOUT)
        except Exception:
            pass
        finally:
            null.close()

    # -- raw access, in the DP's own byte order ------------------------------
    def _read(self, off, n):
        a = self.base + off
        start = a & ~7
        lead = a - start
        span = ((lead + n + 7) // 8) * 8
        raw = bytes(bytearray(self.mm[start:start + span]))
        return swap8(raw)[lead:lead + n]

    def _write(self, off, data):
        a = self.base + off
        start = a & ~7
        lead = a - start
        span = ((lead + len(data) + 7) // 8) * 8
        cur = bytearray(swap8(bytes(bytearray(self.mm[start:start + span]))))
        cur[lead:lead + len(data)] = bytearray(data)
        self.mm[start:start + span] = swap8(bytes(cur))

    def u32(self, off):
        return struct.unpack(">I", self._read(off, 4))[0]

    def set_u32(self, off, v):
        self._write(off, struct.pack(">I", v))

    # -- protocol -----------------------------------------------------------
    def status(self):
        magic = self._read(OFF_MAGIC, 8)
        if magic == b"\xff" * 8:
            return None, "window not answering (all-ones)"
        if magic != MAGIC:
            return None, "no agent: magic is %r" % magic
        return (self.u32(OFF_VER), self.u32(OFF_UP), self.u32(OFF_CMD_SEQ),
                self.u32(OFF_RSP_SEQ)), None

    def run(self, cmd, timeout=30.0):
        raw = cmd if isinstance(cmd, bytes) else cmd.encode()
        if len(raw) > CMD_MAX:
            raise ValueError("command too long (%d > %d)" % (len(raw), CMD_MAX))
        seq = (self.u32(OFF_CMD_SEQ) + 1) & 0xFFFFFFFF
        self._write(OFF_CMD, raw)             # payload
        self.set_u32(OFF_CMD_LEN, len(raw))   # then length
        self.set_u32(OFF_CMD_SEQ, seq)        # sequence LAST

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.u32(OFF_RSP_SEQ) == seq:
                n = self.u32(OFF_RSP_LEN)
                if n > RSP_MAX:
                    n = RSP_MAX
                out = self._read(OFF_RSP, n) if n else b""
                st = self.u32(OFF_RSP_STAT)
                if st >= 0x80000000:
                    st -= 0x100000000
                return out.decode("ascii", "replace"), st
            time.sleep(0.05)
        raise RuntimeError("DP agent did not answer within %.0fs "
                           "(is /sbin/ffn_dpagent running?)" % timeout)


def main():
    ap = argparse.ArgumentParser(description="shell on the DP over PCIe")
    ap.add_argument("-c", "--command")
    ap.add_argument("-t", "--timeout", type=float, default=30.0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--skip-window", action="store_true",
                    help="assume PEM0_BAR1_INDEX1 is already programmed")
    a = ap.parse_args()

    try:
        r = Ring(program_window=not a.skip_window)
    except Exception as exc:
        print("ffn-dpsh: cannot map the DP window: %s" % exc)
        return 2

    try:
        st, err = r.status()
        if err:
            print("ffn-dpsh: %s" % err)
            print("  the DP must be booted with /sbin/ffn_dpagent running")
            return 2
        if a.status:
            print("agent up: version %d, up=%d, cmd_seq=%d, rsp_seq=%d" % st)
            return 0

        if a.command:
            out, rc = r.run(a.command, a.timeout)
            sys.stdout.write(out)
            if out and not out.endswith("\n"):
                sys.stdout.write("\n")
            return 0 if rc == 0 else (rc & 0xFF or 1)

        print("ffn-dpsh: DP shell over PCIe. ctrl-D or 'exit' to leave.")
        prompt = getattr(__builtins__, "raw_input", None) or input
        while True:
            try:
                line = prompt("dp# ")
            except EOFError:
                print()
                break
            if line.strip() in ("exit", "quit"):
                break
            if not line.strip():
                continue
            try:
                out, rc = r.run(line, a.timeout)
            except Exception as exc:
                print("ffn-dpsh: %s" % exc)
                continue
            sys.stdout.write(out)
            if out and not out.endswith("\n"):
                sys.stdout.write("\n")
            if rc != 0:
                print("[exit %d]" % rc)
        return 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
