#!/usr/bin/env python3
"""Prove the ffn_pcnet ring protocol end to end, without an OCTEON boot.

Host side reaches the region through the index-1 BAR window (fast, direct MMIO).
The OCTEON side is stood in for by ffn_cpdp's memrdblk/memwrblk -- which run ON
the OCTEON and touch its LOCAL DRAM -- so this is a genuine test of
host-BAR-write against OCTEON-CPU-read (and the reverse), i.e. the real
cross-PCIe coherency, not two host views of the same bytes.

Passing here means the format, the CRC, and the head/tail discipline are correct
before a single line of OCTEON C is written.
"""
import mmap
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, ".")
import ffn_octdram as od
import ffn_pcnet_ring as pn

BAR2_SLICE = 0x400000                 # index-1 aperture base within BAR2
PCI = "0000:01:00.0"


# ---- host side: direct MMIO through the index-1 window --------------------
def make_host():
    idxval = ((pn.BASE >> 22) << 4) | 3
    be = od.VendorCsrBackend()
    got = be.write("spem0_bar1_index1", idxval)
    if got != idxval:
        raise RuntimeError("could not point index1 (wrote 0x%x got %s)"
                           % (idxval, got))
    fd = os.open("/sys/bus/pci/devices/%s/resource2" % PCI, os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x800000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def rd(off, n):
        return bytes(m[BAR2_SLICE + off:BAR2_SLICE + off + n])

    def wr(off, data):
        m[BAR2_SLICE + off:BAR2_SLICE + off + len(data)] = data

    return rd, wr, (lambda: (m.close(), os.close(fd)))


# ---- OCTEON side: through cpdp, which executes on the OCTEON --------------
def make_oct():
    def rd(off, n):
        with tempfile.NamedTemporaryFile(delete=False) as t:
            path = t.name
        try:
            subprocess.run(["python3", "ffn_cpdp.py", "memrdblk",
                            "0x%x" % (pn.BASE + off), str(n), "--out", path],
                           check=True, capture_output=True, timeout=30)
            with open(path, "rb") as fh:
                return fh.read()[:n]
        finally:
            os.unlink(path)

    def wr(off, data):
        with tempfile.NamedTemporaryFile(delete=False) as t:
            t.write(data)
            path = t.name
        try:
            subprocess.run(["python3", "ffn_cpdp.py", "memwrblk",
                            "0x%x" % (pn.BASE + off), path],
                           check=True, capture_output=True, timeout=30)
        finally:
            os.unlink(path)

    return rd, wr


def main():
    hrd, hwr, hclose = make_host()
    ord_, owr = make_oct()

    # host owns region init; octeon waits for the magic, then attaches
    pn.host_init(hrd, hwr)
    import struct as _s, time as _t
    for _ in range(30):
        if _s.unpack(">Q", ord_(0, 8))[0] == pn.MAGIC:
            break
        _t.sleep(0.1)
    pn.oct_attach(ord_, owr)

    # Wait for the peer's up-flag to become visible. A host BAR read can outrun
    # the OCTEON's cached write reaching the L2 coherence point, so the host
    # daemon polls for the peer rather than sampling once -- the same wait it
    # will do at real startup.
    import time
    magic = ver = nsl = sl = hup = oup = 0
    for _ in range(30):
        magic, ver, nsl, sl, h2o, o2h, hup, oup = pn.read_hdr(hrd)
        if magic == pn.MAGIC and hup and oup:
            break
        time.sleep(0.1)
    print("  header: magic=%s ver=%d nslots=%d slot=%d host_up=%d oct_up=%d"
          % ("OK" if magic == pn.MAGIC else "BAD", ver, nsl, sl, hup, oup))
    if magic != pn.MAGIC or (hup, oup) != (1, 1):
        print("  FAIL: header/up-flags wrong"); return 1

    # host produces on H2O, oct consumes; oct produces on O2H, host consumes
    h2o_host = pn.Ring(pn.H2O_OFF, hrd, hwr)
    h2o_oct = pn.Ring(pn.H2O_OFF, ord_, owr)
    o2h_oct = pn.Ring(pn.O2H_OFF, ord_, owr)
    o2h_host = pn.Ring(pn.O2H_OFF, hrd, hwr)

    ok = True

    # A: host -> OCTEON, several frames including a full-MTU one
    print("  --- H2O: host writes (BAR), OCTEON reads (cpdp) ---")
    sent = [b"ping-1", b"the quick brown fox" * 3, bytes(range(256)) * 5][:3]
    for f in sent:
        if not h2o_host.put(f):
            print("  FAIL: H2O put returned full"); ok = False
    for i, expect in enumerate(sent):
        got = h2o_oct.get()
        good = got == expect
        ok &= good
        print("    frame %d  %d bytes  %s" % (i, len(expect),
              "OK" if good else "MISMATCH got=%r" % (got[:20] if got else None)))
    if h2o_oct.get() is not None:
        print("  FAIL: H2O not empty after draining"); ok = False

    # B: OCTEON -> host
    print("  --- O2H: OCTEON writes (cpdp), host reads (BAR) ---")
    reply = [b"pong-1", b"reply payload of moderate length 0123456789"][:2]
    for f in reply:
        if not o2h_oct.put(f):
            print("  FAIL: O2H put full"); ok = False
    for i, expect in enumerate(reply):
        got = o2h_host.get()
        good = got == expect
        ok &= good
        print("    frame %d  %d bytes  %s" % (i, len(expect),
              "OK" if good else "MISMATCH got=%r" % (got[:20] if got else None)))

    hclose()
    print("  ==> %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
