#!/usr/bin/env python3
"""ffn_octdram -- host access to OCTEON CSRs and DRAM, FFN's own implementation.

Why this module exists
----------------------
FFN assumed the 64 MB BAR window was a flat map of Octeon DRAM. It is not.
Staging 46 MB at 0x400000 through that assumption "succeeded" and read back
with a different hash, because host access to DRAM goes through a **BAR1 index
register** that maps one 4 MB segment at a time.

Everything here was read out of the vendor's own host library
(liboct-remote_mp.so.1, shipped unstripped with debug_info):

  pci_bar1_setup   @0x19360   the index register write
  pci_write_mem    @0x194b0   the 4 MB segment walk
  pci_read_mem     @0x19530
  __pci_read_csr_wrapper  @0x1a1c0   the BAR0 window protocol
  pci_open         @0x1ad50   per-family register offsets

The BAR1 index encoding (identical in both of pci_bar1_setup's paths):

    segment = addr >> 22                  # 4 MB granularity
    value   = (segment << 4) | 3          # low bits 0b11 = enable/valid

written either as a 32-bit store to a BAR0 offset (older parts) or as a CSR
write (CN7XXX). Either way it is followed by a read-back of the same register,
which flushes the posted write -- skip that and the mapping may not be in
effect when the copy starts.

CSR access over PCIe, from __pci_read_csr_wrapper: the address is written as
two 32-bit halves, **high at +4 first, then low at +0**, each followed by a
read-back; then the data is read high at +4, low at +0, and combined
(hi << 32) | lo.

Register offsets are family-dependent and are NOT guessed here -- see
WIN_SETS. The CN7XXX set is derived exactly as pci_open does it, and
`validate()` proves the read path against live silicon before anything writes.
"""
import mmap
import os
import struct
import sys

SYSFS = "/sys/bus/pci/devices"

BAR1_SEG_SHIFT = 22                     # 4 MB segments
BAR1_SEG_SIZE = 1 << BAR1_SEG_SHIFT
BAR1_SEG_MASK = BAR1_SEG_SIZE - 1
BAR1_VALID_BITS = 0x3                   # the |3 in pci_bar1_setup

# pci_open computes these from cvmx_get_proc_id(). Reproduced, not invented:
#   t = (proc_id ^ 0x80d9500) & 0xffff38
#   sel = 0xffffffff if t == 0 else 0          (the sbb)
#   win_rd_addr = (sel & 0xfffe0000) + 0x20010
#   win_rd_data = (sel & 0xfffe0000) + 0x20040
#   win_wr_data = (sel & 0xfffe0000) + 0x20020
#   win_wr_mask = (sel & 0xfffe0000) + 0x20030
#   win_wr_addr = (sel & 0x20000)
# all truncated to 32 bits.
#
# NOTE the asymmetry: win_wr_addr uses a different mask and no addend, so for a
# part where sel == 0 it lands on 0 while its siblings land at 0x2xxxx. That is
# what the binary says; it is flagged rather than "corrected", and `validate()`
# exists precisely because a wrong offset here would mean writing into an
# unknown register.
# NOT CONFIRMED. The offsets below are what pci_open computes, but they do NOT
# reproduce known-good values: reading SPEM0_BAR1_INDEX0 through them returns
# 0x60 where the vendor tool reads 0x3, and every index in the array reads the
# same 0x60. A sweep over plausible (rd_addr, rd_data) pairs -- the legacy set,
# the 0x200-range set, 32-bit halves in both orders, and single 64-bit accesses
# in both endiannesses -- reproduced the ground truth in NO case. So the host
# CSR access path is not yet understood, and csr_write() must not be used.
#
# What this means practically: FFN knows WHICH register to program
# (SPEM0_BAR1_INDEX0) and WHAT to write ((addr >> 22) << 4 | 3), but not yet HOW
# to reach it from the host. Until that is resolved, stage through the vendor
# oct-remote-load and use FFN only for mailbox commands, which are verified.
SPEM0_BAR1_INDEX0 = 0x11800C0000100
SPEM_BAR1_INDEX_STRIDE = 8
SPEM_BAR1_INDEX_COUNT = 16

LEGACY_WINDOW = {"wr_addr": 0x00, "wr_data": 0x10, "wr_mask": 0x18,
                 "rd_addr": 0x08, "rd_data": 0x20}


def window_offsets(proc_id):
    """The BAR0 window register offsets for this part, as pci_open derives them."""
    t = (proc_id ^ 0x80D9500) & 0xFFFF38
    sel = 0xFFFFFFFF if t == 0 else 0
    m = lambda base: ((sel & 0xFFFE0000) + base) & 0xFFFFFFFF
    return {"rd_addr": m(0x20010), "rd_data": m(0x20040),
            "wr_data": m(0x20020), "wr_mask": m(0x20030),
            "wr_addr": (sel & 0x20000) & 0xFFFFFFFF}


def bar1_index_csr(proc_id, pcie_port=0):
    """The BAR1 index CSR address, for parts that reach it as a CSR.

    pci_open: (0x230018000020 + (port << 21)) << 3, and the CSR path is taken
    when bit 48 of that value is set (the testb on byte 6).

    CONFIRMED for CN73XX: this yields 0x11800c0000100, which the vendor's own
    CSR database names **SPEM0_BAR1_INDEX0** -- SPEM, not PEM, is the
    host-facing endpoint on this part, which is why no pem0_bar1_index exists.
    Stride is 8 bytes per index, 16 indexes: 0x...100 .. 0x...178.
    Ground truth from oct-remote-csr on the live box:
        SPEM0_BAR1_INDEX0 = 0x3   (ADDR_IDX 0, low bits 3 -> segment 0, valid)
        SPEM0_BAR1_INDEX1..15 = 0x0
    0x3 == (0 << 4) | 3 exactly matches bar1_index_value(0), and the window
    does map DRAM segment 0 -- the mailbox is readable at window 0x6c000. So
    the ADDRESS and the ENCODING are both confirmed.
    """
    fam = proc_id & 0xFFFF00
    if fam not in (0xD9700, 0xD9800):
        return None
    return (0x230018000020 + (pcie_port << 21)) << 3


def bar1_index_is_csr(value):
    return bool((value >> 48) & 1)


def bar1_index_value(addr):
    """The value pci_bar1_setup writes for `addr`."""
    return (((addr >> BAR1_SEG_SHIFT) << 4) | BAR1_VALID_BITS)


class OctDram:
    """CSR and DRAM access for one OCTEON PCIe endpoint.

    Requires the function to be OUT of vfio-pci and in D0 -- the vendor tools
    reach BAR0/BAR1 the same way. Rebinding vfio-pci parks it in D3hot and
    resets it, which discards a running bootloader.
    """

    def __init__(self, pci="0000:01:00.0", bar0=0, bar1=2, proc_id=None,
                 pcie_port=0):
        self.pci = pci
        self._b0i, self._b1i = bar0, bar1
        self.proc_id = proc_id if proc_id is not None else self._proc_id()
        self.win = window_offsets(self.proc_id)
        self.bar1_csr = bar1_index_csr(self.proc_id, pcie_port)
        self._fd0 = self._fd1 = None
        self._mm0 = self._mm1 = None
        self._mapped_seg = None

    # -- discovery ---------------------------------------------------------
    def _proc_id(self):
        """proc_id from PCI device id + revision.

        The vendor tool prints "Octeon model is 0xd9700" then refines it to
        0x000d9703 -- device id 0x9700 supplies the family, and PCI config
        byte 8 (revision) the pass. No CSR read needed, which matters because
        CSR access is what we are trying to establish.
        """
        base = os.path.join(SYSFS, self.pci)
        dev = int(open(os.path.join(base, "device")).read().strip(), 16)
        rev = 0
        try:
            with open(os.path.join(base, "config"), "rb") as f:
                f.seek(8)
                rev = f.read(1)[0]
        except OSError:
            pass
        return 0xD0000 | (dev & 0xFF00) | (rev & 0xFF)

    def _size(self, idx):
        with open(os.path.join(SYSFS, self.pci, "resource")) as f:
            for i, ln in enumerate(f):
                if i != idx:
                    continue
                p = ln.split()
                s, e = int(p[0], 16), int(p[1], 16)
                return (e - s + 1) if e > s else 0
        return 0

    def __enter__(self):
        base = os.path.join(SYSFS, self.pci)
        s0, s1 = self._size(self._b0i), self._size(self._b1i)
        if not s0 or not s1:
            raise RuntimeError("BAR%d/BAR%d not present on %s"
                               % (self._b0i, self._b1i, self.pci))
        self.bar1_size = s1
        self._fd0 = os.open(os.path.join(base, "resource%d" % self._b0i),
                            os.O_RDWR | getattr(os, "O_SYNC", 0))
        self._mm0 = mmap.mmap(self._fd0, s0, mmap.MAP_SHARED,
                              mmap.PROT_READ | mmap.PROT_WRITE)
        self._fd1 = os.open(os.path.join(base, "resource%d" % self._b1i),
                            os.O_RDWR | getattr(os, "O_SYNC", 0))
        self._mm1 = mmap.mmap(self._fd1, s1, mmap.MAP_SHARED,
                              mmap.PROT_READ | mmap.PROT_WRITE)
        return self

    def __exit__(self, *a):
        for mm in (self._mm0, self._mm1):
            if mm:
                mm.close()
        for fd in (self._fd0, self._fd1):
            if fd is not None:
                os.close(fd)

    # -- BAR0 32-bit primitives (little-endian, as the host sees the window) --
    def _r32(self, off):
        return struct.unpack("<I", self._mm0[off:off + 4])[0]

    def _w32(self, off, val):
        self._mm0[off:off + 4] = struct.pack("<I", val & 0xFFFFFFFF)

    # -- CSR access --------------------------------------------------------
    def csr_read(self, csr):
        """Read a 64-bit Octeon CSR through the BAR0 window."""
        a, d = self.win["rd_addr"], self.win["rd_data"]
        self._w32(a + 4, (csr >> 32) & 0xFFFFFFFF)
        self._r32(a + 4)                       # read-back flush, as the vendor does
        self._w32(a, csr & 0xFFFFFFFF)
        self._r32(a)
        hi = self._r32(d + 4)
        lo = self._r32(d)
        return (hi << 32) | lo

    def csr_write(self, csr, val):
        """Write a 64-bit Octeon CSR through the BAR0 window."""
        a, d = self.win["wr_addr"], self.win["wr_data"]
        self._w32(a + 4, (csr >> 32) & 0xFFFFFFFF)
        self._r32(a + 4)
        self._w32(a, csr & 0xFFFFFFFF)
        self._r32(a)
        self._w32(d + 4, (val >> 32) & 0xFFFFFFFF)
        self._w32(d, val & 0xFFFFFFFF)

    # -- BAR1 windowing ----------------------------------------------------
    def bar1_setup(self, addr, force=False):
        """Map the 4 MB segment containing `addr` into the BAR1 window."""
        if self.bar1_csr is None:
            raise RuntimeError("no BAR1 index CSR known for proc_id 0x%x"
                               % self.proc_id)
        val = bar1_index_value(addr)
        if not force:
            return val
        self.csr_write(self.bar1_csr, val)
        self.csr_read(self.bar1_csr)           # read-back flush
        self._mapped_seg = addr >> BAR1_SEG_SHIFT
        return val

    def write_mem(self, addr, data, force=False):
        """Write into Octeon DRAM, walking 4 MB segments -- pci_write_mem."""
        end = addr + len(data)
        cur, src = addr, 0
        spans = []
        while cur < end:
            seg_end = (cur & ~BAR1_SEG_MASK) + BAR1_SEG_SIZE
            n = min(end, seg_end) - cur
            win_off = cur & BAR1_SEG_MASK
            spans.append((cur, win_off, n))
            if force:
                self.bar1_setup(cur, force=True)
                self._mm1[win_off:win_off + n] = data[src:src + n]
            cur += n
            src += n
        return spans

    def read_mem(self, addr, length, force=False):
        """Read from Octeon DRAM, walking 4 MB segments -- pci_read_mem."""
        out = bytearray()
        end = addr + length
        cur = addr
        while cur < end:
            seg_end = (cur & ~BAR1_SEG_MASK) + BAR1_SEG_SIZE
            n = min(end, seg_end) - cur
            win_off = cur & BAR1_SEG_MASK
            if force:
                self.bar1_setup(cur, force=True)
                out += bytes(self._mm1[win_off:win_off + n])
            cur += n
        return bytes(out)

    # -- validation --------------------------------------------------------
    def validate(self):
        """Prove the READ path against live silicon before trusting the write
        path. Reads only."""
        out = {"pci": self.pci, "proc_id": "0x%x" % self.proc_id,
               "window": {k: "0x%x" % v for k, v in self.win.items()},
               "bar1_index_csr": ("0x%x" % self.bar1_csr) if self.bar1_csr
                                 else None,
               "bar1_is_csr": bar1_index_is_csr(self.bar1_csr or 0)}
        if self.bar1_csr is None:
            out["verdict"] = "no CSR path known for this part"
            return out
        raw = self.csr_read(self.bar1_csr)
        out["bar1_index_reads"] = "0x%x" % raw
        # A programmed index looks like (seg << 4) | 3.
        plausible = (raw & 0x3) == 0x3 and raw != 0xFFFFFFFFFFFFFFFF and raw != 0
        out["looks_programmed"] = plausible
        if plausible:
            out["mapped_segment"] = "0x%x" % (raw >> 4)
            out["maps_dram_at"] = "0x%x" % ((raw >> 4) << BAR1_SEG_SHIFT)
        # Independent read-path proof. The vendor's own oct-remote-csr reports
        # PEM1/2/3_BAR1_INDEX0 as 0xffffffffffffffff on this box (those PEMs are
        # not the host link, so the reads go nowhere). If our window protocol is
        # right we must reproduce exactly that for the same addresses -- a check
        # that does not depend on knowing what PEM0 holds.
        probes = {"PEM1_BAR1_INDEX0": 0x11800C1000100,
                  "PEM2_BAR1_INDEX0": 0x11800C2000100,
                  "PEM3_BAR1_INDEX0": 0x11800C3000100}
        got = {k: self.csr_read(v) for k, v in probes.items()}
        out["read_path_probe"] = {k: "0x%x" % v for k, v in got.items()}
        # An all-ones result only proves the read FAILED somewhere -- far too
        # weak. The real test is a register whose value we know: the vendor tool
        # reads SPEM0_BAR1_INDEX0 = 0x3 on this box.
        out["weak_probe_all_ones"] = all(v == 0xFFFFFFFFFFFFFFFF
                                          for v in got.values())
        truth = self.csr_read(SPEM0_BAR1_INDEX0)
        out["spem0_index0_reads"] = "0x%x" % truth
        out["spem0_index0_vendor"] = "0x3"
        rp = (truth == 0x3)
        out["read_path_confirmed"] = rp

        # PEM stride, from the vendor tool: PEM_n at 0x11800c{n}000100.
        out["pem0_index0_expected_at"] = "0x11800c0000100"
        out["target_register"] = ("SPEM0_BAR1_INDEX0 @0x11800c0000100 "
                                  "(CONFIRMED by name in the vendor CSR db; "
                                  "vendor reads 0x3 = segment 0, valid)")
        out["csr_access_confirmed"] = False
        out["note"] = ("the register and its encoding are confirmed; the host "
                       "CSR ACCESS path is not -- our read returns 0x60 where "
                       "the vendor reads 0x3, and no offset/width/endian "
                       "variant tried reproduces ground truth. csr_write is "
                       "therefore unsafe to use.")

        if not rp:
            out["verdict"] = ("read path NOT confirmed: SPEM0_BAR1_INDEX0 reads "
                              "%s here but 0x3 via the vendor tool, so the host "
                              "CSR access path is wrong. DO NOT WRITE."
                              % out["spem0_index0_reads"])
        else:
            out["verdict"] = ("read path CONFIRMED against known-good value -- "
                              "write path may be armed")
        out["write_path_armed"] = bool(rp)
        return out



# ---------------------------------------------------------------------------
# BAR1 windowing, working path.
#
# FFN's own host CSR access is still wrong (see validate(): our read returns
# 0x60 where the vendor reads 0x3). But the register and the encoding ARE
# confirmed, and the owner's `oct-remote-csr` can WRITE a CSR by name:
#
#     oct-remote-csr [--devnum=N] CSR [value]
#
# So the missing piece was never the algorithm -- pci_write_mem's 4 MB segment
# walk was already implemented here -- only a CSR backend that works. Routing
# the index writes through the vendor tool makes staging work today.
#
# Proven on hardware: writing SPEM0_BAR1_INDEX0 = 0x13 (segment 1) made the
# bootloader mailbox at window 0x6c000 stop reading 2, and restoring 0x3
# brought it straight back -- the window really is paged, and the write really
# takes effect.
#
# This is slow: one subprocess per 4 MB segment. Fine for staging a bitstream,
# not for a data path. When FFN's native CSR access is fixed, swap the backend
# and the segment walk is unchanged.
# ---------------------------------------------------------------------------

VENDOR_TOOLS = "/var/lib/ffn-ngfw/vendor/gryphon-tools/octtools"
_CSR_LINE = __import__("re").compile(
    r"^([A-Z0-9_]+)\((0x[0-9a-f]+)\)\s*=\s*(0x[0-9a-f]+)")
_ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")

SPEM0_BAR1_INDEX0_NAME = "spem0_bar1_index0"


class VendorCsrBackend:
    """CSR read/write through the owner's oct-remote-csr, by NAME.

    Names rather than addresses, because that is the tool's interface and it
    carries a per-model database -- which is how SPEM0_BAR1_INDEX0 was found
    in the first place (there is no pem0_bar1_index on CN73XX).
    """

    def __init__(self, tools=VENDOR_TOOLS, devnum=None):
        self.tools = tools
        self.devnum = devnum
        self.exe = os.path.join(tools, "oct-remote-csr")
        self.available = os.path.exists(self.exe)

    def _run(self, name, value=None):
        import subprocess
        if not self.available:
            return None
        args = [self.exe]
        if self.devnum is not None:
            args.append("--devnum=%d" % self.devnum)
        args.append(name)
        if value is not None:
            args.append("0x%x" % value)
        env = dict(os.environ, LD_LIBRARY_PATH=self.tools)
        try:
            r = subprocess.run(args, env=env, cwd=self.tools,
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        for ln in _ANSI.sub("", r.stdout + r.stderr).splitlines():
            m = _CSR_LINE.match(ln.strip())
            if m:
                return int(m.group(3), 16)
        return None

    def read(self, name):
        return self._run(name)

    def write(self, name, value):
        """Write, then read back -- the read-back is the posted-write flush
        pci_bar1_setup also performs."""
        self._run(name, value)
        return self._run(name)


class WindowedDram:
    """Stage data into Octeon DRAM through the paged BAR1 window.

    Implements pci_write_mem's algorithm with a working CSR backend:
    per 4 MB segment, point the index register at it and copy into the LOW
    4 MB of the window.
    """

    def __init__(self, pci="0000:01:00.0", bar=2, backend=None,
                 index_csr=SPEM0_BAR1_INDEX0_NAME):
        self.pci = pci
        self.bar = bar
        self.csr = backend or VendorCsrBackend()
        self.index_csr = index_csr
        self._fd = self._mm = None
        self._saved = None

    def __enter__(self):
        path = os.path.join(SYSFS, self.pci, "resource%d" % self.bar)
        self._fd = os.open(path, os.O_RDWR | getattr(os, "O_SYNC", 0))
        self._mm = mmap.mmap(self._fd, BAR1_SEG_SIZE, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)
        self._saved = self.csr.read(self.index_csr)
        return self

    def __exit__(self, *a):
        # Always put the window back: segment 0 is where the boot mailbox
        # lives, and leaving it pointing elsewhere blinds every other tool.
        if self._saved is not None:
            self.csr.write(self.index_csr, self._saved)
        if self._mm:
            self._mm.close()
        if self._fd is not None:
            os.close(self._fd)

    def map_segment(self, addr):
        want = bar1_index_value(addr)
        got = self.csr.write(self.index_csr, want)
        if got != want:
            raise RuntimeError("index register did not take 0x%x (read 0x%x) "
                               "-- refusing to copy into an unknown mapping"
                               % (want, got if got is not None else -1))
        return want

    def write(self, addr, data, progress=None):
        """Copy `data` to Octeon physical `addr`, walking segments."""
        end = addr + len(data)
        cur, src, nseg = addr, 0, 0
        while cur < end:
            seg_end = (cur & ~BAR1_SEG_MASK) + BAR1_SEG_SIZE
            n = min(end, seg_end) - cur
            self.map_segment(cur)
            off = cur & BAR1_SEG_MASK
            self._mm[off:off + n] = data[src:src + n]
            cur += n
            src += n
            nseg += 1
            if progress:
                progress(src, len(data), nseg)
        return nseg

    def read(self, addr, length, progress=None):
        out = bytearray()
        end = addr + length
        cur, nseg = addr, 0
        while cur < end:
            seg_end = (cur & ~BAR1_SEG_MASK) + BAR1_SEG_SIZE
            n = min(end, seg_end) - cur
            self.map_segment(cur)
            off = cur & BAR1_SEG_MASK
            out += bytes(self._mm[off:off + n])
            cur += n
            nseg += 1
            if progress:
                progress(len(out), length, nseg)
        return bytes(out)

def main():
    a = sys.argv[1:]
    pci = a[a.index("--pci") + 1] if "--pci" in a else "0000:01:00.0"
    if "--stage" in a:
        try:
            path = a[a.index("--stage") + 1]
            addr = int(a[a.index("--addr") + 1], 0) if "--addr" in a else 0x400000
        except (IndexError, ValueError):
            print("usage: --stage <file> [--addr 0xN] [--verify]")
            return 2
        data = open(path, "rb").read()
        import hashlib
        want = hashlib.sha256(data).hexdigest()
        print("staging %s (%.2f MiB) at 0x%x" % (path, len(data) / (1 << 20),
                                                 addr))
        print("  sha256 %s" % want)

        def prog(done, total, nseg):
            if nseg % 4 == 0 or done == total:
                print("    %6.2f%%  segment %d" % (100.0 * done / total, nseg))

        with WindowedDram(pci) as w:
            if not w.csr.available:
                print("oct-remote-csr absent -- no working CSR backend")
                return 2
            nseg = w.write(addr, data, progress=prog)
            print("  wrote %d segment(s)" % nseg)
            if "--verify" in a:
                back = w.read(addr, len(data))
                got = hashlib.sha256(back).hexdigest()
                print("  readback sha256 %s" % got)
                print("  MATCH" if got == want else "  MISMATCH")
                return 0 if got == want else 1
        return 0

    with OctDram(pci) as d:
        import json
        print(json.dumps(d.validate(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
