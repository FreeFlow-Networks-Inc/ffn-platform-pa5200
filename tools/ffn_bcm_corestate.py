#!/usr/bin/env python3
"""
Report the state of a bcm.user core at an _iproc_read fault.

Run on the MP: python3 ffn_bcm_corestate.py /opt/dpfs/tmp/core.bcm.user.<pid>

Why this exists: the interesting numbers are the iProc base and the RESIDUAL
(addr - _iproc_offset()), and the raw fault address is useless on its own
because the /dev/mem mmap base moves every run. See the ffn-bcm-iproc-window-
protocol memory for the mechanism.
"""
import struct
import sys

EPC_IPROC_READ = 0x104F1BB8   # linux-user-bde.c:1872, the faulting load
GOT = 0x1A5E0000              # solved from the iproc_map symbol + 17752
BAR0_APERTURE = 0x8000        # BAR0 is 32 KB; a valid residual is < this


def main(path):
    f = open(path, "rb")
    hdr = f.read(64)
    if hdr[:4] != b"\x7fELF":
        sys.exit("not an ELF core")
    phoff = struct.unpack(">Q", hdr[0x20:0x28])[0]
    phentsize, phnum = struct.unpack(">HH", hdr[0x36:0x3A])
    f.seek(phoff)
    ph = f.read(phentsize * phnum)

    segs, notes = [], []
    for i in range(phnum):
        e = ph[i * phentsize:(i + 1) * phentsize]
        typ = struct.unpack(">I", e[0:4])[0]
        off, va = struct.unpack(">QQ", e[0x08:0x18])
        fsz = struct.unpack(">Q", e[0x20:0x28])[0]
        if typ == 1 and fsz:
            segs.append((va, fsz, off))
        elif typ == 4:
            notes.append((off, fsz))

    def rd(v, n):
        for base, sz, off in segs:
            if base <= v and v + n <= base + sz:
                f.seek(off + (v - base))
                return f.read(n)
        return None

    # --- notes: NT_PRSTATUS (1) and NT_FILE (0x46494c45) ---
    prstatus = ntfile = None
    noff, nsz = notes[0]
    f.seek(noff)
    nd = f.read(nsz)
    p = 0
    while p + 12 <= len(nd):
        nsl, dsl, ntype = struct.unpack(">III", nd[p:p + 12])
        p += 12
        p += (nsl + 3) & ~3
        val = nd[p:p + dsl]
        p += (dsl + 3) & ~3
        if ntype == 1 and prstatus is None:
            prstatus = val
        elif ntype == 0x46494C45 and ntfile is None:
            ntfile = val

    # The MIPS64 gregset does NOT sit at the offset the generic elf_prstatus
    # layout implies (64 here, not 112). Calibrate against the known EPC.
    gb = None
    for o in range(0, len(prstatus) - 8, 8):
        if struct.unpack(">Q", prstatus[o:o + 8])[0] == EPC_IPROC_READ:
            gb = o - 40 * 8
            break
    if gb is None or gb < 0:
        sys.exit("EPC 0x%x not in NT_PRSTATUS -- not an _iproc_read fault?"
                 % EPC_IPROC_READ)

    reg = lambda i: struct.unpack(">Q", prstatus[gb + i * 8:gb + i * 8 + 8])[0]
    epc, bad, s8 = reg(40), reg(41), reg(6 + 30)
    print("gregset offset %d   epc 0x%x   badvaddr 0x%x" % (gb, epc, bad))

    # --- the BAR windows, straight out of NT_FILE ---
    if ntfile:
        cnt, _psz = struct.unpack(">QQ", ntfile[0:16])
        ents = ntfile[16:16 + cnt * 24]
        names = ntfile[16 + cnt * 24:].split(b"\0")
        print("\nmapped windows:")
        for i in range(cnt):
            st, en, po = struct.unpack(">QQQ", ents[i * 24:i * 24 + 24])
            nm = names[i].decode("latin1") if i < len(names) else "?"
            if "/dev/mem" in nm:
                print("  0x%012x +0x%-8x phys 0x%011x  %s"
                      % (st, en - st, po * 4096, nm))

    # --- the per-device struct the fault used ---
    dp = rd(GOT - 17984, 22 * 8)
    dev0 = struct.unpack(">Q", dp[0:8])[0] if dp else 0
    iproc = 0
    if dev0:
        st = rd(dev0, 0x40)
        if st:
            iproc = struct.unpack(">Q", st[0x38:0x40])[0]
    print("\ndevs[0] = 0x%x   iproc_base (+0x38) = 0x%x" % (dev0, iproc))

    # --- the verdict ---
    frame = rd(s8, 0x40)
    resid = struct.unpack(">I", frame[0x14:0x18])[0] if frame else None
    dev = struct.unpack(">I", frame[0x10:0x14])[0] if frame else None
    print("\ndev index = %s" % dev)
    if resid is not None:
        print("residual  = 0x%08x   (aperture is 0x%x)" % (resid, BAR0_APERTURE))
        if resid < BAR0_APERTURE:
            print("  -> INSIDE the aperture: the window mechanism is working;"
                  " look elsewhere for the fault")
        else:
            page = resid // 0x1000
            print("  -> OUTSIDE by %.1fx. iProc window NOT programmed."
                  % (resid / BAR0_APERTURE))
            print("     would be BAR0 page %d; only pages 0-7 exist" % page)
    if iproc and resid is not None:
        chk = iproc + (resid & ~3)
        print("\ncheck: 0x%x + 0x%x = 0x%x (badvaddr 0x%x) %s"
              % (iproc, resid & ~3, chk, bad, "OK" if chk == bad else "MISMATCH"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/dpfs/tmp/core.bcm.user")
