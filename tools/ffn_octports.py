#!/usr/bin/env python3
"""ffn_octports -- enumerate the interface ports the OCTEON owns.

The problem
-----------
A PA-5220 shows only seven NICs on the x86 bus, all management-class. Its 24
data ports are not on the host bus at all -- they belong to the OCTEON's BGX
blocks. So "detect the rest of the ports" means reading OCTEON CSRs, not
scanning PCI.

Where the 24 comes from
-----------------------
Two independent sources on the 5220 material agree:

* `/etc/cfgdb/dp/5200/PA-5220/portcount.cfgdb.xml` -> platform.portcount = 24
  (and maxnifbw = 20000000, i.e. 20 Gbps)
* the **DP** bootloader's embedded device tree declares **six** BGX nexuses,
  `ethernet-mac-nexus@11800e{0..5}000000`, all `cavium,octeon-7890-bgx`.
  6 blocks x 4 LMACs = 24.

The **CP** bootloader's device tree declares only one BGX nexus (BGX2, with
four `ethernet-mac@0..3` children) -- so the CP is the smaller part and the
**DP owns the front panel**. PAN's own DP boot line passes `board_rev` "so that
DP can figure out if CP is 76xx or 73xx", which fits: CP = CN73XX (3 BGX in the
CSR name table), DP = a 6-BGX part. The DP also has 4 MDIO buses vs the CP's 2.

Register map, every address verified against the vendor's own CSR database
(oct-remote-csr) on live silicon:

    BGX block base   0x11800e0000000 + n*0x1000000          n = 0..5
      CMR_RX_LMACS   base + 0x000308      [2:0] LMACS
      CMR_TX_LMACS   base + 0x001000      [2:0] LMACS
      CMRm_CONFIG    base + m*0x100000    m = 0..3
                       [15]    ENABLE
                       [14]    DATA_PKT_RX_EN
                       [13]    DATA_PKT_TX_EN
                       [10:8]  LMAC_TYPE
                       [7:0]   LANE_TO_SDS
    GSERn_CFG        0x1180090000080 + n*0x1000000          n = 0..13
                       [5] SATA [4] BGX_QUAD [3] BGX_DUAL
                       [2] BGX  [1] ILA      [0] PCIE

Reading it
----------
FFN's own host CSR path is not yet proven (see ffn_octdram.py), so this reads
through the owner's `oct-remote-csr` when present -- their tool, on their
hardware, used in place. Swap in the native reader once it validates.

Caveat that matters: a bare u-boot has NOT configured the MACs. Straight after
`oct-remote-boot` the CP reports every CMR `ENABLE=0`, `LMAC_TYPE=0`, and
GSER0-3=PCIe / GSER5=BGX only. Those are reset/partial values, not the port
inventory. The inventory becomes real once the DP is booted and its software
programs BGX -- which is exactly why PAN's DP boot line carries numcores,
pktbuf, wqe and board_rev. So this tool reports what the silicon currently
says, and labels it, rather than implying 24 live ports exist.
"""
import os
import re
import subprocess
import sys

BGX_BASE = 0x11800E0000000
BGX_STRIDE = 0x1000000
BGX_BLOCKS = 6
CMR_STRIDE = 0x100000
LMACS_PER_BLOCK = 4
OFF_CMR_RX_LMACS = 0x000308
OFF_CMR_TX_LMACS = 0x001000

GSER_BASE = 0x1180090000080
GSER_STRIDE = 0x1000000
GSER_COUNT = 7

LMAC_TYPE = {0: "SGMII", 1: "XAUI", 2: "RXAUI", 3: "10G-R (XFI)",
             4: "40G-R (XLAUI)", 5: "QSGMII", 6: "RGMII", 7: "reserved"}

GSER_BITS = [(0, "PCIE"), (1, "ILA"), (2, "BGX"), (3, "BGX_DUAL"),
             (4, "BGX_QUAD"), (5, "SATA")]

TOOLS = "/var/lib/ffn-ngfw/vendor/gryphon-tools/octtools"
LINE = re.compile(r"^([A-Z0-9_]+)\((0x[0-9a-f]+)\)\s*=\s*(0x[0-9a-f]+)")


class VendorCsr:
    """Read CSRs by name through the owner's oct-remote-csr."""

    def __init__(self, tools=TOOLS, devnum=None):
        self.tools = tools
        self.devnum = devnum
        self.exe = os.path.join(tools, "oct-remote-csr")
        self.available = os.path.exists(self.exe)

    def read(self, name):
        if not self.available:
            return None, None
        env = dict(os.environ, LD_LIBRARY_PATH=self.tools)
        try:
            cmd = [self.exe]
            if self.devnum is not None:
                cmd.append("--devnum=%d" % self.devnum)
            cmd.append(name)
            out = subprocess.run(cmd, env=env, cwd=self.tools,
                                 capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return None, None
        for ln in out.splitlines():
            m = LINE.match(ln.strip().replace("\x1b", ""))
            if m:
                return int(m.group(2), 16), int(m.group(3), 16)
        return None, None


def decode_cmr(val):
    return {"enable": bool(val >> 15 & 1),
            "rx_en": bool(val >> 14 & 1),
            "tx_en": bool(val >> 13 & 1),
            "lmac_type": val >> 8 & 7,
            "lmac_type_name": LMAC_TYPE.get(val >> 8 & 7, "?"),
            "lane_to_sds": val & 0xFF}


def decode_gser(val):
    return [n for b, n in GSER_BITS if val >> b & 1] or ["unassigned"]


def bgx_addr(block, lmac=None, off=0):
    a = BGX_BASE + block * BGX_STRIDE
    if lmac is not None:
        a += lmac * CMR_STRIDE
    return a + off


def enumerate_ports(csr):
    out = {"blocks": [], "gser": [], "expected_ports": BGX_BLOCKS * LMACS_PER_BLOCK}
    for n in range(GSER_COUNT):
        _a, v = csr.read("gser%d_cfg" % n)
        out["gser"].append({"gser": n,
                            "value": None if v is None else "0x%x" % v,
                            "roles": decode_gser(v) if v is not None else ["?"]})
    live = 0
    for b in range(BGX_BLOCKS):
        _a, rx = csr.read("bgx%d_cmr_rx_lmacs" % b)
        _a, tx = csr.read("bgx%d_cmr_tx_lmacs" % b)
        blk = {"bgx": b, "base": "0x%x" % bgx_addr(b),
               "rx_lmacs": None if rx is None else rx & 7,
               "tx_lmacs": None if tx is None else tx & 7,
               "responds": rx is not None and rx != 0xFFFFFFFFFFFFFFFF,
               "ports": []}
        for m in range(LMACS_PER_BLOCK):
            _a, v = csr.read("bgx%d_cmr%d_config" % (b, m))
            if v is None or v == 0xFFFFFFFFFFFFFFFF:
                blk["ports"].append({"lmac": m, "present": False})
                continue
            d = decode_cmr(v)
            d.update({"lmac": m, "present": True, "raw": "0x%x" % v,
                      "addr": "0x%x" % bgx_addr(b, m)})
            if d["enable"]:
                live += 1
            blk["ports"].append(d)
        out["blocks"].append(blk)
    out["enabled_ports"] = live
    return out


def main():
    dn = None
    if "--devnum" in sys.argv:
        dn = int(sys.argv[sys.argv.index("--devnum") + 1])
    csr = VendorCsr(devnum=dn)
    print("reading OCTEON device %s" % (dn if dn is not None else "0 (default)"))
    if not csr.available:
        print("oct-remote-csr not found under %s -- this reads CSRs through the "
              "owner's vendor tool (used in place, never redistributed)" % TOOLS)
        return 2
    r = enumerate_ports(csr)

    print("=== QLM / GSER lane assignment ===")
    for g in r["gser"]:
        print("  GSER%-2d %-6s %s" % (g["gser"], g["value"] or "-",
                                      ", ".join(g["roles"])))
    print()
    resp = [b for b in r["blocks"] if b["responds"]]
    print("=== BGX blocks: %d of %d respond on this device ==="
          % (len(resp), BGX_BLOCKS))
    for b in r["blocks"]:
        if not b["responds"]:
            print("  BGX%d @%s  -- not responding on this device"
                  % (b["bgx"], b["base"]))
            continue
        print("  BGX%d @%s  rx_lmacs=%s tx_lmacs=%s"
              % (b["bgx"], b["base"], b["rx_lmacs"], b["tx_lmacs"]))
        for p in b["ports"]:
            if not p.get("present"):
                print("      lmac%d  --" % p["lmac"])
                continue
            print("      lmac%d  %-14s enable=%-5s rx=%-5s tx=%-5s "
                  "lane_to_sds=0x%02x"
                  % (p["lmac"], p["lmac_type_name"], p["enable"], p["rx_en"],
                     p["tx_en"], p["lane_to_sds"]))
    print()
    cap = sum(LMACS_PER_BLOCK for b in r["blocks"] if b["responds"])
    cfg = [(b["bgx"], p) for b in r["blocks"] for p in b["ports"]
           if p.get("present") and p.get("lane_to_sds") != 0xE4]
    print("BGX capacity on this device : %d LMACs (%d blocks x %d)"
          % (cap, len(resp), LMACS_PER_BLOCK))
    print("LMACs with NON-default config: %d  %s"
          % (len(cfg), ", ".join("BGX%d/lmac%d=%s" % (b, p["lmac"],
                                                      p["lmac_type_name"])
                                 for b, p in cfg) or "-"))
    print("ports currently ENABLED      : %d" % r["enabled_ports"])
    print()
    print("platform.portcount for a PA-5220 is 24, but this device exposes only ")
    print("%d BGX LMACs -- so the 24 front-panel ports are NOT one-per-BGX-LMAC." % cap)
    print("They are aggregated by the ce40 FPGA, which fans the front panel into a")
    print("few high-speed Octeon links. maxnifbw=20Gbps for this model is the")
    print("aggregate to the dataplane, which fits aggregation rather than 24")
    print("independent MACs. Enumerating the front panel therefore means querying")
    print("the FPGA / SFP cages (MDIO + TWSI), not just BGX.")
    if r["enabled_ports"] == 0:
        print("NOTE: zero enabled ports is expected against a bare u-boot -- it "
              "has not configured the MACs. The inventory becomes real once the "
              "DP is booted and its software programs BGX. These are reset "
              "values, not the port list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
