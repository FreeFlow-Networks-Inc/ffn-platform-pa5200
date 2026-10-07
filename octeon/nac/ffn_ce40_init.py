#!/usr/bin/env python3
"""ffn_ce40_init -- the CE40's post-boot initialisation, in FFN's own code.

Runs on the control plane after Linux is up, once per boot. The FPGA load in
u-boot (tools/ffn-fpga-step.sh) leaves every CE40 register at its reset value,
and the one that configures the Interlaken PHY towards the FE100 reads 0xeec.
The vendor's board agent writes 0x72 there as the very first thing it does on
every boot (ce40_init.c:232, recovered from DWARF -- see CE40-BRINGUP.md), and
that write is lost on every reprogram, so it belongs here, not in u-boot.

Order is the vendor's own:
    read 0x008, keep bits 31:30 as the DDR layout selector
    read 0x000 as the FPGA version
    write CE40_ILKPHY_CFG (0x28000) = 0x72
    (vendor: nac_capabilities; FFN: nothing yet)

Binding: ffn_nac.ko claims feed:a00d directly. Until it is staged on the CP,
ffn_ca1.ko is bound through PCI new_id -- same register map, proven on
silicon. Writes need the driver loaded with allow_write=1; the systemd unit
does that, this script does not load modules.

Exit: 0 done (or nothing to do: the socket is not a00d), 1 the write did not
read back, 2 could not try (no device, no driver, no node, writes refused).
Everything observed goes to /run/ffn-ce40-init as JSON for the status layer.
"""
import json
import os
import struct
import sys
import time

PCI = "0003:06:00.0"
SYS = "/sys/bus/pci/devices/" + PCI
STATE = "/run/ffn-ce40-init"
DEVICE_NODES = ("/dev/ffn_nac0", "/dev/ffn_ca10")
DRIVERS = ("ffn_nac", "ffn_ca1")

REG_VERSION = 0x000     # 0x13 = fpga version 19 on the a00d image
REG_LAYOUT = 0x008      # bits 31:30 = DDR layout selector (CE40_MDL_*)
REG_RLDCAL = 0x0b8      # 0xe2 = the value PAN-OS logs as "FPGA RLD CAL 0xe2"
REG_ILKPHY = 0x28000    # CE40_ILKPHY_CFG; three siblings at 0x1c000/0x20000/0x24000
ILKPHY_VALUE = 0x72


def say(msg):
    print("[ce40-init] " + msg, flush=True)


def record(d):
    try:
        with open(STATE, "w") as f:
            json.dump(d, f)
    except OSError:
        pass


def sysfs(name):
    with open(os.path.join(SYS, name)) as f:
        return f.read().strip()


def bound_driver():
    p = os.path.join(SYS, "driver")
    return os.path.basename(os.readlink(p)) if os.path.islink(p) else None


def bind():
    """Return the driver bound to the socket, binding one via new_id if needed."""
    drv = bound_driver()
    if drv:
        return drv
    for name in DRIVERS:
        new_id = "/sys/bus/pci/drivers/%s/new_id" % name
        if not os.path.exists(new_id):
            continue
        try:
            with open(new_id, "w") as f:
                f.write("feed a00d\n")
        except OSError:
            pass
        time.sleep(0.5)
        drv = bound_driver()
        if drv:
            return drv
    return None


def main():
    if not os.path.isdir(SYS):
        say("no device at %s" % PCI)
        record({"ok": False, "why": "no device"})
        return 2
    device = sysfs("device")
    if device != "0xa00d":
        say("socket is %s, not the CE40 personality; nothing to initialise" % device)
        record({"ok": True, "skipped": device})
        return 0
    drv = bind()
    if not drv:
        say("no NAC driver could be bound to %s" % PCI)
        record({"ok": False, "why": "no driver"})
        return 2
    node = next((n for n in DEVICE_NODES if os.path.exists(n)), None)
    if not node:
        say("driver %s bound but no device node in %s" % (drv, DEVICE_NODES))
        record({"ok": False, "why": "no node", "driver": drv})
        return 2
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError as e:
        say("open %s for writing: %s (driver loaded without allow_write=1?)" % (node, e))
        record({"ok": False, "why": "open: %s" % e, "driver": drv})
        return 2

    def rd(off):
        os.lseek(fd, off, 0)
        return struct.unpack(">I", os.read(fd, 4))[0]

    def wr(off, value):
        os.lseek(fd, off, 0)
        return os.write(fd, struct.pack(">I", value))

    version = rd(REG_VERSION)
    layout = rd(REG_LAYOUT) >> 30
    rldcal = rd(REG_RLDCAL)
    before = rd(REG_ILKPHY)
    say("driver %s via %s: fpga version %d, DDR layout %d, RLD cal 0x%02x, ILKPHY_CFG 0x%x"
        % (drv, node, version, layout, rldcal, before))
    try:
        wr(REG_ILKPHY, ILKPHY_VALUE)
    except OSError as e:
        os.close(fd)
        say("write refused: %s (load the driver with allow_write=1)" % e)
        record({"ok": False, "why": "write: %s" % e, "driver": drv,
                "version": version, "layout": layout, "rldcal": rldcal, "ilkphy": before})
        return 2
    after = rd(REG_ILKPHY)
    os.close(fd)
    ok = after == ILKPHY_VALUE
    record({"ok": ok, "driver": drv, "node": node, "version": version, "layout": layout,
            "rldcal": rldcal, "ilkphy_before": before, "ilkphy_after": after})
    say("ILKPHY_CFG 0x%x -> 0x%x: %s" % (before, after, "OK" if ok else "DID NOT TAKE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
