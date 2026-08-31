#!/usr/bin/env python3
"""ffn_gryphon.py -- PA-5200 "Gryphon" hardware bring-up + readiness for FFN.

Purpose: let FFN reclaim EOL PA-5200-class appliances (see the e-waste mission).
On such a box the x86 Xeon-D control plane runs FFN, and the on-board
OCTEON-II NPU + FPGA complex is the line-rate dataplane.

This module is the *bring-up front end*: it detects the Gryphon hardware, checks
every prerequisite for driving it, and reports an actionable readiness checklist.
It deliberately does NOT ship or depend on Palo Alto code.

What the reference platform uses (RE'd read-only from a 9.0.17 PA-5220 image --
see Desktop/PAN/pa5200-brdagent-protocol.md):
  host kernel drivers : pan/pcic.ko, pan/if_pci.ko (net-over-PCIe),
                        pan/pci_dma_host.ko (DMA), pan/nac.ko, pan/fabric_vif.ko,
                        pan/power_ctl.ko, pan/cpld_wdt.ko   <-- VENDOR modules
  host boot utilities : oct-remote-load / oct-remote-bootcmd  <-- these are
                        Cavium/Marvell OCTEON SDK host tools (remote-lib), i.e.
                        a DOCUMENTED mechanism, not a proprietary protocol:
                        load an image into Octeon memory over PCIe, then hand
                        u-boot a boot command.
  octeon side         : u-boot (PCIe EP, CVMX_BOARD_TYPE_MODULE_PCIE_EP_4X) ->
                        MIPS64 Linux -> dataplane app
  fpga                : /boot/fpga/{ca1,ce40}.bin behind a signed manifest

FFN's substitution plan (all FFN's own / open):
  * boot        -> OCTEON SDK host remote-boot equivalent (open SDK tooling)
  * data path   -> upstream endpoint driver (octeon_ep / liquidio family) or
                   vfio-pci + FFN's own DMA ring, replacing pan/if_pci+pci_dma
  * octeon app  -> FFN's own MIPS64 dataplane build (or minimal Linux + DPDK)
  * fpga        -> FFN-signed bitstream manifest (mirrors the FIPS HMAC manifest)

CLI:
    ffn_gryphon.py                 human-readable readiness report
    ffn_gryphon.py --json          machine-readable
    ffn_gryphon.py --publish       push dp.gryphon.* onto the ffn-sysd bus
    ffn_gryphon.py --selftest      logic test (works on non-Gryphon hosts)
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

# Cavium / Marvell PCI vendor id (OCTEON family). 177d is Cavium.
PCI_VENDOR_CAVIUM = "177d"
PCI_VENDOR_XILINX = "10ee"
PCI_VENDOR_ALTERA = "1172"

# Host kernel modules the reference platform loads. These are VENDOR modules:
# FFN does not ship them -- their absence is expected and the report names the
# open substitute for each.
VENDOR_HOST_MODULES = {
    "pcic":        "OCTEON PCIe control",
    "if_pci":      "network-over-PCIe MP<->DP channel",
    "pci_dma":     "host DMA to Octeon memory",
    "nac":         "platform/NAC glue",
    "fabric_vif":  "fabric virtual interface",
    "power_ctl":   "board power control",
    "cpld_wdt":    "CPLD watchdog",
}
# Open/upstream substitutes FFN can actually use.
OPEN_SUBSTITUTES = ("octeon_ep", "liquidio", "vfio-pci", "uio_pci_generic", "igb_uio")

BOOT_ARTIFACT_HINTS = {
    "octeon_uboot":  ["u-boot*octeon*", "u-boot*gryphon*", "u-boot*pciboot*"],
    "octeon_kernel": ["vmlinux*oct*", "vmlinux*-dp"],
    "fpga_bitstream": ["ca1.bin", "ce40.bin", "*.bit", "*.rbf"],
}
BOOT_SEARCH_DIRS = ["/boot", "/boot/fpga", "/opt/dpfs/boot", "/lib/firmware",
                    "/var/lib/ffn-ngfw/gryphon", "/opt/ffn-ngfw-v2/gryphon"]


def _run(cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _read(p, default=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return default


# ---------------------------------------------------------------------------
def detect_octeon_pci():
    """Find OCTEON endpoint(s) on the PCIe bus (sysfs first, lspci for detail)."""
    found = []
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        ven = _read(os.path.join(dev, "vendor")).replace("0x", "").lower()
        if ven != PCI_VENDOR_CAVIUM:
            continue
        addr = os.path.basename(dev)
        drv = ""
        try:
            drv = os.path.basename(os.readlink(os.path.join(dev, "driver")))
        except Exception:
            pass
        found.append({
            "pci": addr,
            "device_id": _read(os.path.join(dev, "device")).replace("0x", ""),
            "class": _read(os.path.join(dev, "class")).replace("0x", ""),
            "driver": drv,
            "bars": _bar_map(dev),
            "numa_node": _read(os.path.join(dev, "numa_node"), "-1"),
        })
    if not found and shutil.which("lspci"):
        for ln in _run(["lspci", "-Dnn"]).splitlines():
            if PCI_VENDOR_CAVIUM in ln.lower() or "cavium" in ln.lower():
                found.append({"pci": ln.split()[0], "device_id": "",
                              "class": "", "driver": "", "bars": [],
                              "numa_node": "-1", "lspci": ln.strip()})
    return found


def _bar_map(devdir):
    """BAR sizes from sysfs `resource` -- the doorbell/shmem windows live here."""
    bars = []
    txt = _read(os.path.join(devdir, "resource"))
    for i, ln in enumerate(txt.splitlines()):
        parts = ln.split()
        if len(parts) < 2:
            continue
        try:
            start, end = int(parts[0], 16), int(parts[1], 16)
        except ValueError:
            continue
        if end > start:
            bars.append({"bar": i, "start": hex(start), "size": (end - start + 1)})
    return bars


def detect_accel_pci():
    out = []
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        ven = _read(os.path.join(dev, "vendor")).replace("0x", "").lower()
        if ven in (PCI_VENDOR_XILINX, PCI_VENDOR_ALTERA):
            out.append({"pci": os.path.basename(dev), "vendor": ven,
                        "device_id": _read(os.path.join(dev, "device")).replace("0x", "")})
    return out


def detect_modules():
    """Which relevant kernel modules are loaded or at least available."""
    loaded = set()
    for ln in _read("/proc/modules").splitlines():
        if ln.strip():
            loaded.add(ln.split()[0])
    avail = set()
    rel = os.uname().release
    for base in ("/lib/modules/%s" % rel, "/usr/lib/modules/%s" % rel):
        for p in glob.glob(base + "/**/*.ko*", recursive=True):
            avail.add(os.path.basename(p).split(".")[0])
    return {
        "vendor": {m: {"desc": d, "loaded": m in loaded, "available": m in avail}
                   for m, d in VENDOR_HOST_MODULES.items()},
        "open_substitutes": {m: {"loaded": m in loaded, "available": m in avail}
                             for m in OPEN_SUBSTITUTES},
    }


def detect_boot_artifacts():
    """Locate Octeon u-boot / kernel / FPGA bitstreams present on THIS box."""
    out = {}
    for kind, pats in BOOT_ARTIFACT_HINTS.items():
        hits = []
        for d in BOOT_SEARCH_DIRS:
            if not os.path.isdir(d):
                continue
            for pat in pats:
                for p in glob.glob(os.path.join(d, pat)):
                    if os.path.isfile(p):
                        hits.append({"path": p, "size": os.path.getsize(p)})
        out[kind] = hits
    return out


def detect_sdk_tools():
    """OCTEON SDK host remote-boot tooling (open SDK, not PAN code)."""
    names = ["oct-remote-load", "oct-remote-bootcmd", "oct-remote-console",
             "ffn-oct-load", "ffn-oct-bootcmd"]
    out = {}
    for n in names:
        p = shutil.which(n)
        if not p:
            for d in ("/opt/ffn-ngfw-v2/gryphon", "/usr/local/bin"):
                cand = os.path.join(d, n)
                if os.path.exists(cand):
                    p = cand
                    break
        out[n] = p or None
    return out


def detect_chassis():
    """Gryphon chassis glue: CPLD/i2c/hwmon presence (informational)."""
    return {
        "i2c_buses": len(glob.glob("/dev/i2c-*")),
        "hwmon": len(glob.glob("/sys/class/hwmon/hwmon*")),
        "gpio_proc": os.path.exists("/proc/gpio"),
        "dmi_product": _read("/sys/class/dmi/id/product_name"),
        "dmi_vendor": _read("/sys/class/dmi/id/sys_vendor"),
    }


def is_gryphon_platform(chassis, octeon):
    """Heuristic: a real Gryphon has an OCTEON endpoint; DMI may say PA-52xx."""
    prod = (chassis.get("dmi_product") or "")
    dmi_hit = bool(re.search(r"PA-52\d0", prod, re.I))
    return {"octeon_present": bool(octeon), "dmi_match": dmi_hit,
            "verdict": bool(octeon) or dmi_hit}


# ---------------------------------------------------------------------------
def detect():
    octeon = detect_octeon_pci()
    chassis = detect_chassis()
    return {
        "octeon": octeon,
        "accelerators": detect_accel_pci(),
        "modules": detect_modules(),
        "boot_artifacts": detect_boot_artifacts(),
        "sdk_tools": detect_sdk_tools(),
        "chassis": chassis,
        "platform": is_gryphon_platform(chassis, octeon),
    }


def readiness(inv=None):
    """Actionable checklist: each item -> ok/missing + the next concrete action."""
    inv = inv or detect()
    items = []

    def add(name, ok, detail, action=""):
        items.append({"check": name, "ok": bool(ok), "detail": detail,
                      "action": ("" if ok else action)})

    oct_ = inv["octeon"]
    add("OCTEON PCIe endpoint present", bool(oct_),
        ("%d device(s): %s" % (len(oct_), ", ".join(d["pci"] for d in oct_)))
        if oct_ else "no Cavium/Marvell (177d) PCI device found",
        "Run FFN on the PA-5200's x86 control plane; the Octeon complex must be "
        "visible on its PCIe bus.")

    bars = oct_[0]["bars"] if oct_ else []
    add("OCTEON BAR windows mapped", bool(bars),
        ", ".join("BAR%d=%dMB" % (b["bar"], b["size"] // (1 << 20)) for b in bars)
        if bars else "no BAR resources read",
        "Check BIOS/PCIe enumeration; BARs carry the shared-memory + doorbell windows.")

    drv = (oct_[0].get("driver") if oct_ else "") or ""
    add("Endpoint bound to a usable driver", bool(drv),
        ("driver=%s" % drv) if drv else "endpoint has no driver bound",
        "Bind vfio-pci (or an octeon_ep/liquidio-family driver) to the endpoint so "
        "FFN can map BARs and DMA. FFN does NOT use PAN's pcic/if_pci/pci_dma.")

    subs = inv["modules"]["open_substitutes"]
    have_sub = [m for m, v in subs.items() if v["loaded"] or v["available"]]
    add("Open PCIe/DMA driver available", bool(have_sub),
        ", ".join(have_sub) if have_sub else "none of %s present" % ", ".join(OPEN_SUBSTITUTES),
        "Install/enable vfio-pci or uio_pci_generic (kernel builtin/module).")

    tools = inv["sdk_tools"]
    have_tools = [k for k, v in tools.items() if v]
    add("OCTEON remote-boot tooling", bool(have_tools),
        ", ".join(have_tools) if have_tools else "no oct-remote-* / ffn-oct-* tooling found",
        "Provide OCTEON SDK host remote-boot equivalents (load image into Octeon "
        "memory over PCIe, then issue a u-boot bootcmd). Open SDK tooling -- do not "
        "copy vendor binaries.")

    ba = inv["boot_artifacts"]
    add("Octeon bootloader image", bool(ba.get("octeon_uboot")),
        "%d found" % len(ba.get("octeon_uboot", [])),
        "Supply a u-boot built for the Octeon PCIe-EP board, or reuse the one "
        "already on the appliance.")
    add("Octeon dataplane kernel/app", bool(ba.get("octeon_kernel")),
        "%d found" % len(ba.get("octeon_kernel", [])),
        "Build FFN's MIPS64 dataplane (or a minimal Linux+DPDK) for the Octeon.")
    add("FPGA bitstream(s)", bool(ba.get("fpga_bitstream")),
        "%d found" % len(ba.get("fpga_bitstream", [])),
        "Stage FFN-signed bitstreams (mirror the FIPS HMAC manifest scheme).")

    ok = sum(1 for i in items if i["ok"])
    total = len(items)
    if not oct_:
        stage = "not-gryphon"
    elif ok == total:
        stage = "ready-to-boot"
    elif ok >= total // 2:
        stage = "partial"
    else:
        stage = "detected-only"
    return {"stage": stage, "passed": ok, "total": total, "checks": items,
            "platform": inv["platform"]}


def publish(inv=None, rdy=None):
    """Push dp.gryphon.* onto the ffn-sysd bus (best-effort)."""
    inv = inv or detect()
    rdy = rdy or readiness(inv)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, "/opt/ffn-ngfw-v2")
        from ffn_sysd import SysdClient
        with SysdClient() as c:
            c.set("dp.gryphon.present", bool(inv["octeon"]))
            c.set("dp.gryphon.stage", rdy["stage"])
            c.set("dp.gryphon.checks_passed", "%d/%d" % (rdy["passed"], rdy["total"]))
            c.set("dp.gryphon.octeon_pci", [d["pci"] for d in inv["octeon"]])
            c.set("dp.gryphon.platform", inv["platform"]["verdict"])
            c.heartbeat("ffn-gryphon")
        return True
    except Exception as e:
        print("sysd publish skipped: %s" % str(e)[:120], file=sys.stderr)
        return False


def _report(inv, rdy):
    p = inv["platform"]
    print("=== FFN Gryphon (PA-5200) bring-up readiness ===")
    print("Chassis : %s %s" % (inv["chassis"]["dmi_vendor"], inv["chassis"]["dmi_product"]))
    print("Platform: octeon_present=%s dmi_match=%s -> %s"
          % (p["octeon_present"], p["dmi_match"],
             "GRYPHON" if p["verdict"] else "not a Gryphon platform"))
    print("Stage   : %s (%d/%d checks)" % (rdy["stage"], rdy["passed"], rdy["total"]))
    print()
    for i in rdy["checks"]:
        print("[%s] %-34s %s" % ("PASS" if i["ok"] else "TODO", i["check"], i["detail"]))
        if i["action"]:
            print("        -> %s" % i["action"])
    vend = [m for m, v in inv["modules"]["vendor"].items() if v["loaded"] or v["available"]]
    if vend:
        print("\nNOTE: vendor host modules present on this box: %s" % ", ".join(vend))
        print("      FFN does not ship or require these; open substitutes are used.")


def _selftest():
    fails = []

    def chk(c, m):
        print(("  ok   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    inv = detect()
    chk(isinstance(inv, dict) and "octeon" in inv, "detect() returns inventory")
    chk(isinstance(inv["octeon"], list), "octeon list present")
    rdy = readiness(inv)
    chk(rdy["total"] >= 8, "readiness has >=8 checks (%d)" % rdy["total"])
    chk(all(("check" in c and "ok" in c and "action" in c) for c in rdy["checks"]),
        "every check has check/ok/action")
    chk(rdy["stage"] in ("not-gryphon", "detected-only", "partial", "ready-to-boot"),
        "stage is a known value (%s)" % rdy["stage"])
    # on a non-Gryphon host the verdict must be honest
    if not inv["octeon"] and not inv["platform"]["dmi_match"]:
        chk(rdy["stage"] == "not-gryphon", "no Octeon -> stage=not-gryphon")
        chk(inv["platform"]["verdict"] is False, "no Octeon -> platform verdict False")
    # failed checks must carry an action
    chk(all(c["action"] for c in rdy["checks"] if not c["ok"]),
        "all failed checks carry a next action")
    chk(isinstance(inv["modules"]["vendor"], dict)
        and "if_pci" in inv["modules"]["vendor"], "vendor module table built")
    chk(isinstance(detect_boot_artifacts(), dict), "boot-artifact scan runs")
    chk(isinstance(detect_sdk_tools(), dict), "sdk tool scan runs")
    print("\n==== ffn_gryphon selftest: %d failed ====" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--selftest":
        sys.exit(_selftest())
    inv = detect()
    rdy = readiness(inv)
    if a and a[0] == "--json":
        print(json.dumps({"inventory": inv, "readiness": rdy}, indent=2))
    elif a and a[0] == "--publish":
        _report(inv, rdy)
        print("\nsysd publish: %s" % ("ok" if publish(inv, rdy) else "skipped"))
    else:
        _report(inv, rdy)
