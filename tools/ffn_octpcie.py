#!/usr/bin/env python3
"""ffn-octpcie -- bring up an OCTEON PCIe root complex from the HOST.

WHY THIS EXISTS
  The FE100 (Broadcom BCM88375) sits behind the OCTEON's PCIe root complex, not
  on the x86 bus. Its 5951 control registers are reachable only once an RC port
  is out of reset, trained, and its config space initialised. u-boot on this
  board never does that (`disable_pci=1`, and patching it out changed nothing),
  and the vendor DP Linux can't be booted because that kernel has no initrd
  support at all. So FFN does it itself.

WHY IT RUNS ON THE HOST
  BAR0 of the OCTEON endpoint is an address/data window onto ANY OCTEON CSR --
  established by disassembling the vendor pcic.ko's pci_read32_csr(). So the
  entire RC bring-up is just a sequence of CSR writes the host can issue. No
  code has to run on the OCTEON, which is what makes this testable at all.

PROVENANCE
  The sequence is ported from OCTEON SDK 5.1 `executive/cvmx-pcie.c`
  (__cvmx_pcie_rc_initialize_config_space, cvmx_pcie_rc_initialize), which
  carries Cavium's inline 3-clause BSD grant -- so this is derived from
  openly-licensed source, not from vendor firmware. Field layouts come from
  cvmx-sli-defs.h / cvmx-pciercx-defs.h (same licence).

  Copyright (c) 2003-2015 Cavium, Inc. for the original sequence (BSD-3-Clause).
  This implementation is FFN's own.

DESIGN NOTE -- fields by name, not by offset
  Rather than hardcode bit positions (which is how you get a silent
  wrong-register bug), this reads the CSR through the vendor tool, parses its
  own field decode to learn each field's bit range, sets the named fields, and
  writes the value back. The register definition therefore always comes from the
  tool that knows this silicon, and the code reads like the SDK source.
"""
import argparse
import os
import re
import subprocess
import sys
import time

VEND = "/var/lib/ffn-ngfw/vendor/gryphon-tools/octtools"
ENV = dict(os.environ, LD_LIBRARY_PATH=VEND)

# "  [41:39] PORT   =   1 (0x1)"  or  "  [   31] EN  =  1 (0x1)"
ALL_ONES = 0xFFFFFFFFFFFFFFFF

FIELD_RE = re.compile(r"^\s*\[\s*(\d+)\s*(?::\s*(\d+))?\s*\]\s+(\S+)\s+=")
VALUE_RE = re.compile(r"=\s*(0x[0-9a-fA-F]+)\s*$")


def csr_read(name, dev=0, timeout=25):
    """-> (u64 value, {FIELD: (hi, lo)}) or (None, {})."""
    r = subprocess.run([os.path.join(VEND, "oct-remote-csr"),
                        "--devnum=%d" % dev, name],
                       capture_output=True, text=True, env=ENV,
                       timeout=timeout)
    lines = r.stdout.splitlines()
    if not lines:
        return None, {}
    m = VALUE_RE.search(lines[0])
    if not m:
        return None, {}
    val = int(m.group(1), 16)
    # All-ones is NOT data -- it is what this silicon returns when a read does
    # not complete (e.g. the PEM core is still in reset). Doing a
    # read-modify-write on 0xffff...ffff would splatter every field with 1s, so
    # refuse to treat it as a value.
    if val == ALL_ONES:
        return None, {}
    fields = {}
    for ln in lines[1:]:
        fm = FIELD_RE.match(ln)
        if fm:
            hi = int(fm.group(1))
            lo = int(fm.group(2)) if fm.group(2) else hi
            fields[fm.group(3).upper()] = (hi, lo)
    return val, fields


def csr_write(name, val, dev=0, timeout=25):
    subprocess.run([os.path.join(VEND, "oct-remote-csr"),
                    "--devnum=%d" % dev, name, "0x%x" % val],
                   capture_output=True, text=True, env=ENV, timeout=timeout)


def set_fields(name, assignments, dev=0, dry=False):
    """Read-modify-write a CSR, setting fields BY NAME.

    Returns (old, new, missing[]) so a caller can see exactly what changed and
    complain loudly if a field name did not exist on this silicon.
    """
    old, fields = csr_read(name, dev)
    if old is None:
        return None, None, ["<unreadable>"]
    new = old
    missing = []
    for fname, fval in assignments.items():
        key = fname.upper()
        if key not in fields:
            missing.append(fname)
            continue
        hi, lo = fields[key]
        mask = ((1 << (hi - lo + 1)) - 1) << lo
        new = (new & ~mask) | ((fval << lo) & mask)
    if not dry and new != old:
        csr_write(name, new, dev)
    return old, new, missing


def step(msg):
    print("  %s" % msg)


def bringup(port, dev=0, dry=False):
    """Port __cvmx_pcie_rc_initialize + _config_space for one RC port."""
    print("=== PCIe RC bring-up: port %d%s ===" % (port, " (DRY RUN)" if dry else ""))

    # ---- 1. release the soft PCIe reset -------------------------------------
    # Verified earlier: PEM1/2/3 sit with RST_SOFT_PRST=1 (asserted) while the
    # host-facing PEM0 has 0. Polarity confirmed by that contrast, not guessed.
    rst = csr_read("rst_ctl%d" % port, dev)[0]
    step("rst_ctl%d before = 0x%x" % (port, rst if rst is not None else 0))
    if not dry:
        csr_write("rst_soft_prst%d" % port, 0, dev)
        time.sleep(2)
    val, f = csr_read("rst_ctl%d" % port, dev)
    done = None
    if val is not None and "RST_DONE" in f:
        hi, lo = f["RST_DONE"]
        done = (val >> lo) & 1
        step("RST_DONE = %d" % done)
    if done != 1:
        if dry:
            step("RST_DONE is 0 -- a real run would deassert PERST first.")
            step("STOPPING the dry run here: every register behind the PEM")
            step("reset reads all-ones, so anything printed past this point")
            step("would be fiction rather than a preview.")
            return 0
        step("PERST deassert did not take (RST_DONE != 1) -- aborting")
        return 1

    # ---- 2. enable link training -------------------------------------------
    old, new, miss = set_fields("pem%d_ctl_status" % port, {"LNK_ENB": 1},
                                dev, dry)
    step("pem%d_ctl_status 0x%x -> 0x%x %s"
         % (port, old or 0, new or 0, "MISSING:%s" % miss if miss else ""))
    if not dry:
        time.sleep(4)

    # ---- 3. wait for the data link to come up ------------------------------
    for _ in range(10):
        val, f = csr_read("pcierc%d_cfg032" % port, dev)
        # csr_read already rejects all-ones, so a value here is real. Without
        # that guard this reported "DLLA=1 width=x63 speed=gen15" off a failed
        # read -- a false link-up, which is worse than no reading at all.
        if val is not None and "DLLA" in f:
            hi, lo = f["DLLA"]
            if (val >> lo) & 1:
                nlw = ls = 0
                if "NLW" in f:
                    h, l = f["NLW"]; nlw = (val >> l) & ((1 << (h - l + 1)) - 1)
                if "LS" in f:
                    h, l = f["LS"]; ls = (val >> l) & ((1 << (h - l + 1)) - 1)
                step("link UP: DLLA=1 width=x%d speed=gen%d" % (nlw, ls))
                break
        if dry:
            break
        time.sleep(1)
    else:
        step("link did NOT come up (DLLA stayed 0) -- nothing downstream?")

    # ---- 4. config space init (from __cvmx_pcie_rc_initialize_config_space) --
    # Order and field choices follow the SDK. MPS/MRRS 0 == 128 bytes, which the
    # SDK picks for best OCTEON DMA performance.
    step("config space:")
    for name, assigns in (
        ("pcierc%d_cfg030" % port, {"MPS": 0, "MRRS": 0, "RO_EN": 1, "NS_EN": 1,
                                    "CE_EN": 1, "NFE_EN": 1, "FE_EN": 1,
                                    "UR_EN": 1}),
        ("pcierc%d_cfg070" % port, {"GE": 1, "CE": 1}),
        # MSAE/ME are the ones that matter most: without memory-space enable
        # and bus-master the RC will not forward memory accesses at all.
        ("pcierc%d_cfg001" % port, {"MSAE": 1, "ME": 1, "I_DIS": 1, "SEE": 1}),
        # Disable the MMIO and prefetchable BARs by making limit < base.
        ("pcierc%d_cfg008" % port, {"MB_ADDR": 0x100, "ML_ADDR": 0}),
        ("pcierc%d_cfg009" % port, {"LMEM_BASE": 0x100, "LMEM_LIMIT": 0}),
        ("pcierc%d_cfg010" % port, {"UMEM_BASE": 0x100}),
        ("pcierc%d_cfg011" % port, {"UMEM_LIMIT": 0}),
        ("pcierc%d_cfg035" % port, {"SECEE": 1, "SEFEE": 1, "SENFEE": 1,
                                    "PMEIE": 1}),
        ("pcierc%d_cfg075" % port, {"CERE": 1, "NFERE": 1, "FERE": 1}),
    ):
        old, new, miss = set_fields(name, assigns, dev, dry)
        if old is None:
            step("  %-18s UNREADABLE" % name)
        else:
            step("  %-18s 0x%08x -> 0x%08x%s"
                 % (name, old, new,
                    "  MISSING:%s" % ",".join(miss) if miss else ""))

    # ---- 4b. gen2 speed selection (SDK: right after config space) ----------
    old, new, miss = set_fields("pcierc%d_cfg515" % port, {"DSC": 1}, dev, dry)
    step("pcierc%d_cfg515 0x%x -> 0x%x  (DSC: gen2 speed selection)%s"
         % (port, old or 0, new or 0, "  MISSING:%s" % miss if miss else ""))

    # ---- 4c. errata ---------------------------------------------------------
    # PEM-28816: a link retrain initiated at GEN1 can wedge the PEM. The SDK
    # applies this only when the negotiated speed is gen1 -- which is exactly
    # what these links negotiate here, so it is not optional for us.
    val, f = csr_read("pcierc%d_cfg032" % port, dev)
    ls = None
    if val is not None and "LS" in f:
        h, l = f["LS"]
        ls = (val >> l) & ((1 << (h - l + 1)) - 1)
    if ls == 1 or dry:
        old, new, miss = set_fields("pcierc%d_cfg548" % port, {"ED": 1},
                                    dev, dry)
        step("pcierc%d_cfg548 0x%x -> 0x%x  (errata PEM-28816, link is gen1)%s"
             % (port, old or 0, new or 0, "  MISSING:%s" % miss if miss else ""))
    else:
        step("pcierc%d_cfg548 skipped (link speed gen%s, errata is gen1-only)"
             % (port, ls))

    # PCIE-29440: needed for atomic operations to work properly.
    old, new, miss = set_fields("pcierc%d_cfg038" % port,
                                {"ATOM_OP_EB": 0, "ATOM_OP": 1}, dev, dry)
    step("pcierc%d_cfg038 0x%x -> 0x%x  (errata PCIE-29440, atomics)%s"
         % (port, old or 0, new or 0, "  MISSING:%s" % miss if miss else ""))

    # ---- 5. DPI/SLI per-port config ----------------------------------------
    old, new, miss = set_fields("dpi_sli_prt%d_cfg" % port,
                                {"MPS": 0, "MRRS": 0, "MOLR": 32}, dev, dry)
    step("dpi_sli_prt%d_cfg 0x%x -> 0x%x%s"
         % (port, old or 0, new or 0, "  MISSING:%s" % miss if miss else ""))

    # ---- 5b. SLI_WINDOW_CTL, errata PEM-31375 ------------------------------
    # "PEM RSL accesses to PCLK registers can timeout during speed change."
    # SDK: TIME = sclk_rate * 525 / 1000000. RST_BOOT[PNR_MUL] on this board is
    # 20 against a 50 MHz reference, so SCLK = 1000 MHz and TIME = 525. Read
    # PNR_MUL live rather than hardcoding, so this stays correct on other boards.
    pnr = None
    val, f = csr_read("rst_boot", dev)
    if val is not None and "PNR_MUL" in f:
        h, l = f["PNR_MUL"]
        pnr = (val >> l) & ((1 << (h - l + 1)) - 1)
    if pnr:
        sclk_mhz = 50 * pnr
        time_val = int(sclk_mhz * 1000000 * 525 / 1000000)
        old, new, miss = set_fields("sli_window_ctl", {"TIME": time_val},
                                    dev, dry)
        step("sli_window_ctl 0x%x -> 0x%x  (TIME=%d from SCLK %d MHz)%s"
             % (old or 0, new or 0, time_val, sclk_mhz,
                "  MISSING:%s" % miss if miss else ""))
    else:
        step("sli_window_ctl SKIPPED -- could not read RST_BOOT[PNR_MUL]")

    # ---- 6. SLI store-merge control (global) -------------------------------
    old, new, miss = set_fields("sli_mem_access_ctl",
                                {"MAX_WORD": 0, "TIMER": 127}, dev, dry)
    step("sli_mem_access_ctl 0x%x -> 0x%x%s"
         % (old or 0, new or 0, "  MISSING:%s" % miss if miss else ""))

    # ---- 7. mem-access SubDIDs for this port -------------------------------
    # SDK: for (i = 12 + port*4; i < 16 + port*4; i++), incrementing the bus
    # address each time so the four windows tile a larger range. CN73XX layout:
    # port:3 @[41:39], esr:2 @[37:36], esw:2 @[35:34], wtype/rtype, ba:30 @[29:0].
    # esr/esw = _CVMX_PCIE_ES = 1 for this big-endian build.
    step("sli_mem_access_subid%d..%d:" % (12 + port * 4, 15 + port * 4))
    for n, i in enumerate(range(12 + port * 4, 16 + port * 4)):
        old, new, miss = set_fields(
            "sli_mem_access_subid%d" % i,
            {"PORT": port, "NMERGE": 0, "ESR": 1, "ESW": 1,
             "WTYPE": 0, "RTYPE": 0, "BA": n},
            dev, dry)
        if old is None:
            step("  subid%-2d UNREADABLE" % i)
        else:
            step("  subid%-2d 0x%016x -> 0x%016x%s"
                 % (i, old, new, "  MISSING:%s" % ",".join(miss) if miss else ""))

    # ---- 7b. disable peer-to-peer forwarding -------------------------------
    # SDK writes PEMX_P2P_BARX_START(i, port) = -1 for i in 0..3. The comment
    # is explicit that the OS sets these up after it enumerates and assigns
    # addresses -- so leaving them at power-on values means the PEM may claim
    # or misroute addresses that should go downstream. A prime suspect for
    # config reads returning a constant regardless of bus/dev.
    step("p2p forwarding disable (bar0..3 start = -1):")
    for i in range(4):
        name = "pem%d_p2p_bar%d_start" % (port, i)
        if not dry:
            csr_write(name, ALL_ONES, dev)
        step("  %-24s <- 0xffffffffffffffff" % name)

    # ---- 8. bus numbers ----------------------------------------------------
    old, new, miss = set_fields("pcierc%d_cfg006" % port,
                                {"PBNUM": 0, "SBNUM": 1, "SUBBNUM": 0xff},
                                dev, dry)
    step("pcierc%d_cfg006 0x%x -> 0x%x%s"
         % (port, old or 0, new or 0, "  MISSING:%s" % miss if miss else ""))

    return 0


def main():
    ap = argparse.ArgumentParser(
        description="bring up an OCTEON PCIe root complex from the host")
    ap.add_argument("--port", type=int, required=True, choices=[1, 2, 3],
                    help="RC port (PEM1/2/3; PEM0 is the host endpoint)")
    ap.add_argument("--dev", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="read and show what WOULD change, write nothing")
    a = ap.parse_args()
    if not os.path.isdir(VEND):
        print("vendor octtools not found at %s" % VEND, file=sys.stderr)
        return 2
    return bringup(a.port, a.dev, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
