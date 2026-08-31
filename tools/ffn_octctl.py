#!/usr/bin/env python3
"""ffn-octctl -- manage and operate the OCTEON + FPGA complex.

One operational front end over work that had accumulated across several
modules. Every operation names the backend it used, because FFN's own path is
proven for some things and not for others, and quietly falling back would hide
that:

  operation                backend            proven on hardware?
  -----------------------  -----------------  ---------------------------
  endpoint / power / BARs  FFN (vfio)         yes -- D3hot trap found this way
  reset + bootloader load  vendor oct-remote  yes -- "Powering up cores"
  mailbox commands         FFN own code       yes -- bootloader consumed them
  image staging (<=4 MB)   FFN own code       yes, within the mapped window
  image staging (>4 MB)    --                 NO: needs SPEM0_BAR1_INDEX
  CSR reads                vendor oct-remote  yes -- cross-checked
  CSR reads                FFN own code       NO: reads 0x60 vs vendor 0x3
  port inventory           vendor CSR reads   yes
  FPGA program             FFN cmd + mailbox  command verified, not yet run

Vendor tools are used IN PLACE on hardware whose owner already has them; they
are never packaged or redistributed.

The Octeon must be out of vfio-pci and in D0 for the vendor tools to reach it
(they predate vfio and use /proc/bus/pci + /dev/mem). `boot` arranges that.
Do NOT rebind vfio-pci afterwards: that parks the function in D3hot and resets
it, discarding a running bootloader.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, "/opt/ffn-ngfw-v2")

TOOLS = "/var/lib/ffn-ngfw/vendor/gryphon-tools/octtools"
VENDOR_FW = "/var/lib/ffn-ngfw/vendor/gryphon"
SYSFS = "/sys/bus/pci/devices"
CSR_LINE = re.compile(r"^([A-Z0-9_]+)\((0x[0-9a-f]+)\)\s*=\s*(0x[0-9a-f]+)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# BGX / GSER map -- addresses verified against the vendor CSR database
BGX_BASE, BGX_STRIDE, CMR_STRIDE = 0x11800E0000000, 0x1000000, 0x100000
LMAC_TYPE = {0: "SGMII", 1: "XAUI", 2: "RXAUI", 3: "10G-R", 4: "40G-R",
             5: "QSGMII", 6: "RGMII", 7: "rsvd"}
GSER_ROLES = [(0, "PCIE"), (1, "ILA"), (2, "BGX"), (3, "BGX_DUAL"),
              (4, "BGX_QUAD"), (5, "SATA")]

MBOX_STATE = 0x6C000
STATE_READY = 2


def _read(p, d=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return d


def octeon_endpoints():
    out = []
    for d in sorted(os.listdir(SYSFS)):
        base = os.path.join(SYSFS, d)
        if _read(os.path.join(base, "vendor")).lower() != "0x177d":
            continue
        drv = ""
        try:
            drv = os.path.basename(os.readlink(os.path.join(base, "driver")))
        except OSError:
            pass
        out.append({"pci": d, "device": _read(os.path.join(base, "device")),
                    "driver": drv or None,
                    "power": _read(os.path.join(base, "power_state"), "?"),
                    "enabled": _read(os.path.join(base, "enable"), "?")})
    return out


class Vendor:
    """The owner's oct-remote-* tools, used in place."""

    def __init__(self, tools=TOOLS):
        self.tools = tools
        self.ok = os.path.isdir(tools)

    def _run(self, prog, args, timeout=300):
        exe = os.path.join(self.tools, prog)
        if not os.path.exists(exe):
            return None, "%s not present" % prog
        env = dict(os.environ, LD_LIBRARY_PATH=self.tools)
        try:
            r = subprocess.run([exe] + args, env=env, cwd=self.tools,
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)
        return ANSI.sub("", r.stdout + r.stderr), None

    def csr(self, name, devnum=None):
        args = (["--devnum=%d" % devnum] if devnum is not None else []) + [name]
        out, err = self._run("oct-remote-csr", args, timeout=60)
        if out is None:
            return None, err
        for ln in out.splitlines():
            m = CSR_LINE.match(ln.strip())
            if m:
                return {"name": m.group(1), "addr": int(m.group(2), 16),
                        "value": int(m.group(3), 16)}, None
        return None, "no CSR line in output"

    def reset(self, devnum=0):
        return self._run("oct-remote-reset", ["--devnum=%d" % devnum, "nowait"],
                         timeout=90)

    def boot(self, image, devnum=0, loadcache=True):
        args = ["--devnum=%d" % devnum]
        if loadcache:
            args.append("--loadcache")
        args.append(image)
        return self._run("oct-remote-boot", args)

    def load(self, addr, path, devnum=0):
        return self._run("oct-remote-load",
                         ["--devnum=%d" % devnum, "0x%x" % addr, path])


def bar_size(pci, idx):
    try:
        with open(os.path.join(SYSFS, pci, "resource")) as f:
            for i, ln in enumerate(f):
                if i != idx:
                    continue
                p = ln.split()
                s, e = int(p[0], 16), int(p[1], 16)
                return (e - s + 1) if e > s else 0
    except OSError:
        pass
    return 0


def mailbox_state(pci, bar=2):
    """Read the bootloader mailbox state word (big-endian, as the target
    stores it). Requires the endpoint out of vfio and in D0."""
    import mmap
    import struct
    path = os.path.join(SYSFS, pci, "resource%d" % bar)
    size = bar_size(pci, bar)
    if not size or not os.path.exists(path):
        return None, "BAR%d absent" % bar
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        return None, str(e)
    try:
        mm = mmap.mmap(fd, min(size, 1 << 20), mmap.MAP_SHARED, mmap.PROT_READ)
    except OSError as e:
        os.close(fd)
        return None, str(e)
    try:
        raw = mm[MBOX_STATE:MBOX_STATE + 4]
        if raw == b"\xff" * 4:
            return None, "window not answering (all-ones)"
        return struct.unpack(">I", raw)[0], None
    finally:
        mm.close()
        os.close(fd)


# ---------------------------------------------------------------- status ----
def cmd_status(a):
    v = Vendor()
    eps = octeon_endpoints()
    print("=== OCTEON endpoints ===")
    if not eps:
        print("  none found (no 177d: device on this host)")
        return 1
    for e in eps:
        print("  %s  dev=%s driver=%-9s power=%-6s enabled=%s"
              % (e["pci"], e["device"], e["driver"] or "-", e["power"],
                 e["enabled"]))
        if e["driver"] == "vfio-pci":
            print("      NOTE: bound to vfio-pci. Unopened vfio devices sit in "
                  "D3hot where every BAR read returns all-ones and writes are "
                  "dropped. Run 'boot' or 'attach' first.")
    print()

    for n, e in enumerate(eps):
        pci = e["pci"]
        print("=== %s (device %d) ===" % (pci, n))
        for idx in (0, 2, 4):
            sz = bar_size(pci, idx)
            if sz:
                print("  BAR%d  %7.2f MiB" % (idx, sz / (1 << 20)))
        st, err = mailbox_state(pci)
        if err:
            print("  bootloader mailbox @0x%x: unreadable (%s)"
                  % (MBOX_STATE, err))
        else:
            print("  bootloader mailbox @0x%x = %d%s"
                  % (MBOX_STATE, st,
                     "  (READY -- a bootloader is running)"
                     if st == STATE_READY else "  (not ready)"))
        # devnum matters: without it every endpoint reports device 0's CSRs,
        # which reads as though they were all identical.
        if v.ok and e["device"] == "0x9700":
            r, err = v.csr("gser5_cfg", n)
            if r:
                roles = [n for b, n in GSER_ROLES if r["value"] >> b & 1]
                print("  GSER5 = 0x%x  %s" % (r["value"],
                                              ", ".join(roles) or "unassigned"))
        print()

    print("=== staged vendor firmware ===")
    if os.path.isdir(VENDOR_FW):
        for f in sorted(os.listdir(VENDOR_FW)):
            p = os.path.join(VENDOR_FW, f)
            if os.path.isfile(p):
                print("  %-40s %8.2f MiB" % (f, os.path.getsize(p) / (1 << 20)))
    else:
        print("  none imported")
    print()
    print("vendor tools: %s" % ("present at " + TOOLS if v.ok else "ABSENT"))
    return 0


# ------------------------------------------------------------------ boot ----
def cmd_boot(a):
    """The sequence PAN-OS itself uses, from /opt/dpfs/sbin/octeon:
       reset --nowait, then oct-remote-boot --loadcache <u-boot>."""
    devnum = int(_opt(a, "--dev", "0"))
    image = _opt(a, "--image",
                 os.path.join(VENDOR_FW, "u-boot-gryphon_cp_pciboot.bin"))
    if not os.path.exists(image):
        print("no bootloader image: %s" % image)
        return 2
    v = Vendor()
    if not v.ok:
        print("vendor tools absent -- FFN cannot perform the initial PCIe boot "
              "itself (that is DDR/L2 bring-up, not reimplemented)")
        return 2

    eps = octeon_endpoints()
    if not eps:
        print("no OCTEON endpoint")
        return 1
    pci = _opt(a, "--pci", eps[min(devnum, len(eps) - 1)]["pci"])

    if "--force" not in a:
        print("would boot device %d (%s) with %s" % (devnum, pci, image))
        print("  1. unbind vfio-pci, enable -> D0")
        print("  2. oct-remote-reset --devnum=%d nowait" % devnum)
        print("  3. oct-remote-boot --devnum=%d --loadcache <image>" % devnum)
        print("pass --force to actually do it")
        return 0

    # 1. vfio out of the way, device in D0
    drv = None
    try:
        drv = os.path.basename(os.readlink(os.path.join(SYSFS, pci, "driver")))
    except OSError:
        pass
    if drv == "vfio-pci":
        try:
            with open("/sys/bus/pci/drivers/vfio-pci/unbind", "w") as f:
                f.write(pci)
            print("[1] unbound %s from vfio-pci" % pci)
        except OSError as e:
            print("[1] unbind failed: %s" % e)
            return 1
    try:
        with open(os.path.join(SYSFS, pci, "enable"), "w") as f:
            f.write("1")
    except OSError:
        pass
    print("[1] power state now %s"
          % _read(os.path.join(SYSFS, pci, "power_state"), "?"))

    out, err = v.reset(devnum)
    print("[2] reset: %s" % (err or _tail(out)))
    out, err = v.boot(image, devnum)
    print("[3] boot: %s" % (err or _tail(out, 4)))

    st, mberr = mailbox_state(pci)
    if mberr:
        print("mailbox: unreadable (%s)" % mberr)
        return 1
    print("mailbox @0x%x = %d%s" % (MBOX_STATE, st,
                                    "  READY" if st == STATE_READY else ""))
    return 0 if st == STATE_READY else 1


# ------------------------------------------------------------------- cmd ----
def cmd_cmd(a):
    """Send a u-boot command through the mailbox -- FFN's own implementation,
    verified against a live bootloader."""
    if not a or a[0].startswith("--"):
        print('usage: ffn_octctl.py cmd "<u-boot command>" '
              '[--pci B:D.F] [--wait SEC]')
        return 2
    text = a[0]
    import ffn_oct
    eps = octeon_endpoints()
    if not eps:
        print("no OCTEON endpoint")
        return 1
    pci = _opt(a, "--pci", eps[0]["pci"])
    bar = int(_opt(a, "--bar", "2"))

    st, err = mailbox_state(pci, bar)
    if err:
        print("mailbox unreadable (%s) -- is a bootloader running?" % err)
        return 1
    if st != STATE_READY:
        print("bootloader not ready (state=%d)" % st)
        return 1

    mem = _SysfsMem(os.path.join(SYSFS, pci, "resource%d" % bar),
                   bar_size(pci, bar))
    try:
        bb = ffn_oct.BootBar(mem, dry_run=False)
        gen = ffn_oct._configured_octeon_gen()
        # Open and drain the console BEFORE sending, or the start of the
        # reply is lost. The mailbox carries the command; the console carries
        # the output. Neither alone is a complete channel.
        cfd = None
        try:
            import ffn_octcon
            if os.path.exists(ffn_octcon.PORT):
                cfd = ffn_octcon.open_console()
                ffn_octcon.drain(cfd)
        except Exception as e:
            print("console unavailable (%s) -- sending blind" % e)
            cfd = None

        ok, msg, tr = ffn_oct.oct_send_bootcmd(bb, text, gen=gen, force=True)
        print("%s: %s" % ("ok" if ok else "FAILED", msg))
        print("trace: %s" % [x[0] for x in tr])

        if cfd is not None:
            try:
                wait = float(_opt(a, "--wait", "6"))
                reply = ffn_octcon.read_reply(cfd, wait=wait, echo_of=text)
                print()
                print("--- console reply (%s) ---" % ffn_octcon.PORT)
                print(reply if reply.strip() else "(no output)")
            finally:
                os.close(cfd)
        else:
            print("NOTE: without the console this reports only that the "
                  "bootloader CONSUMED the command, not what it returned. "
                  "The Octeon console is %s @115200." % (
                      getattr(__import__("ffn_octcon"), "PORT", "/dev/ttyS1")
                      if False else "/dev/ttyS1"))
        return 0 if ok else 1
    finally:
        mem.close()


class _SysfsMem:
    """Slice protocol over a BAR mmap, for ffn_oct.BootBar."""

    def __init__(self, path, size):
        import mmap
        self._fd = os.open(path, os.O_RDWR | getattr(os, "O_SYNC", 0))
        self._mm = mmap.mmap(self._fd, size, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)

    def __len__(self):
        return len(self._mm)

    def __getitem__(self, k):
        return self._mm[k]

    def __setitem__(self, k, v):
        self._mm[k] = v

    def close(self):
        self._mm.close()
        os.close(self._fd)


# ------------------------------------------------------------------ fpga ----
def cmd_fpga(a):
    """Program the FPGA. The bitstream goes into DRAM, then u-boot's own
    fpga_program with load=none programs it from there."""
    import ffn_oct
    bs = _opt(a, "--bitstream", os.path.join(VENDOR_FW, "ce40.bin"))
    if not os.path.exists(bs):
        print("no bitstream: %s" % bs)
        return 2
    size = os.path.getsize(bs)
    addr = int(_opt(a, "--addr", hex(ffn_oct.FPGA_DEFAULT_ADDR)), 0)

    try:
        cmd = ffn_oct.build_fpga_program_cmd(addr=addr, size=size,
                                            force="--reprogram" in a)
    except ValueError as e:
        print("refusing: %s" % e)
        return 2
    print("bitstream : %s (%.2f MiB)" % (bs, size / (1 << 20)))
    ident = ffn_oct.identify_bitstream(bs)
    print("identity  : %s idcode=%s"
          % (ident.get("part"),
             ("0x%08x" % ident["idcode"]) if ident.get("idcode") else "?"))
    print("command   : %s" % cmd)

    conflict = ffn_oct.fpga_region_conflict(addr, size)
    if conflict:
        print("refusing: %s" % conflict)
        return 2

    st, err = mailbox_state(_opt(a, "--pci", "0000:01:00.0"))
    if err or st != STATE_READY:
        print("refusing: bootloader not ready (%s) -- run 'boot' first"
              % (err or ("state=%d" % st)))
        return 1

    if "--force" not in a:
        print()
        print("dry run. --force would:")
        print("  1. stage %.2f MiB into Octeon DRAM at 0x%x, walking 4 MiB "
              "segments" % (size / (1 << 20), addr))
        print("  2. verify the readback by sha256")
        print("  3. send %r through the mailbox" % cmd)
        print("NOTE: the bootloader prints SUCCESS/FAILURE on the Octeon "
              "CONSOLE, which FFN cannot read yet -- so step 3 confirms the "
              "command was consumed, not that the FPGA came up.")
        return 0

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hashlib
    import ffn_octdram as od

    data = open(bs, "rb").read()
    want = hashlib.sha256(data).hexdigest()

    def prog(done, total, nseg):
        if nseg % 4 == 0 or done == total:
            print("      %6.2f%%  segment %d" % (100.0 * done / total, nseg))

    print()
    print("[1] staging into Octeon DRAM at 0x%x" % addr)
    with od.WindowedDram(_opt(a, "--pci", "0000:01:00.0")) as w:
        if not w.csr.available:
            print("    no CSR backend (oct-remote-csr absent) -- cannot page "
                  "the BAR1 window")
            return 2
        nseg = w.write(addr, data, progress=prog)
        print("    wrote %d segment(s)" % nseg)
        print("[2] verifying readback")
        got = hashlib.sha256(w.read(addr, len(data))).hexdigest()
        if got != want:
            print("    MISMATCH (%s) -- refusing to program from a bad stage"
                  % got[:16])
            return 1
        print("    sha256 matches")

    print("[3] sending %r" % cmd)
    pci = _opt(a, "--pci", "0000:01:00.0")
    mem = _SysfsMem(os.path.join(SYSFS, pci, "resource2"), bar_size(pci, 2))
    try:
        bb = ffn_oct.BootBar(mem, dry_run=False)
        ok, msg, _tr = ffn_oct.oct_send_bootcmd(
            bb, cmd, gen=ffn_oct._configured_octeon_gen(), force=True)
        print("    %s: %s" % ("ok" if ok else "FAILED", msg))
    finally:
        mem.close()
    print()
    print("The bootloader retries up to %d times and prints "
          "'Full fpga programming SUCCESS' or FAILURE on its console. That "
          "console line is the real confirmation; this tool can only report "
          "that the command was accepted." % ffn_oct.FPGA_ATTEMPTS)
    return 0 if ok else 1


# ----------------------------------------------------------------- ports ----
def cmd_ports(a):
    v = Vendor()
    if not v.ok:
        print("vendor tools absent -- CSR reads need oct-remote-csr until "
              "FFN's own CSR path validates")
        return 2
    devnum = int(_opt(a, "--dev", "0"))
    print("=== QLM / GSER lane assignment (device %d) ===" % devnum)
    for g in range(7):
        r, err = v.csr("gser%d_cfg" % g, devnum)
        if not r:
            print("  GSER%-2d  unreadable (%s)" % (g, err))
            continue
        roles = [n for b, n in GSER_ROLES if r["value"] >> b & 1]
        print("  GSER%-2d  0x%x  %s"
              % (g, r["value"], ", ".join(roles) or "unassigned"))
    print()
    print("=== BGX blocks ===")
    live = cap = 0
    for b in range(6):
        r, _e = v.csr("bgx%d_cmr_rx_lmacs" % b, devnum)
        if not r or r["value"] == 0xFFFFFFFFFFFFFFFF:
            print("  BGX%d  not present on this device" % b)
            continue
        cap += 4
        print("  BGX%d  rx_lmacs=%d" % (b, r["value"] & 7))
        for m in range(4):
            c, _e = v.csr("bgx%d_cmr%d_config" % (b, m), devnum)
            if not c or c["value"] == 0xFFFFFFFFFFFFFFFF:
                continue
            val = c["value"]
            typ = LMAC_TYPE.get(val >> 8 & 7, "?")
            lane = val & 0xFF
            en = bool(val >> 15 & 1)
            dflt = (lane == 0xE4 and (val >> 8 & 7) == 0)
            if en:
                live += 1
            print("      lmac%d  %-6s lane_to_sds=0x%02x enable=%-5s %s"
                  % (m, typ, lane, en,
                     "(reset default -- unconfigured)" if dflt
                     else "<- configured"))
    print()
    print("BGX capacity %d LMACs, %d enabled" % (cap, live))
    if live == 0:
        print("Zero enabled is expected against a bare u-boot: it has not "
              "programmed the MACs. Note also that the front panel is on the "
              "FE100 (Broadcom Arad-class) ASIC, so these BGX links are the "
              "Octeon's side of that, not the panel itself.")
    return 0


# ------------------------------------------------------------------- csr ----
def cmd_csr(a):
    if not a or a[0].startswith("--"):
        print("usage: ffn_octctl.py csr <name> [--dev N]")
        return 2
    v = Vendor()
    if not v.ok:
        print("vendor tools absent")
        return 2
    dev = _opt(a, "--dev", None)
    r, err = v.csr(a[0], int(dev) if dev else None)
    if not r:
        print("could not read %s: %s" % (a[0], err))
        return 1
    print("%s(0x%x) = 0x%016x" % (r["name"], r["addr"], r["value"]))
    return 0


# ------------------------------------------------------------------------ --
def _opt(a, name, default=None):
    if name in a:
        try:
            return a[a.index(name) + 1]
        except IndexError:
            pass
    return default


def _tail(out, n=1):
    if not out:
        return "(no output)"
    lines = [l for l in out.splitlines() if l.strip()
             and "not recognized" not in l]
    return " | ".join(lines[-n:]) if lines else "(no output)"


CMDS = {"status": cmd_status, "boot": cmd_boot, "cmd": cmd_cmd,
        "fpga": cmd_fpga, "ports": cmd_ports, "csr": cmd_csr}


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help") or a[0] not in CMDS:
        print(__doc__.strip().split("\n")[0])
        print()
        print("usage: ffn_octctl.py <command> [options]")
        print()
        print("  status                 endpoints, BARs, mailbox, firmware")
        print("  boot   [--dev N] [--image F] [--force]")
        print("                         reset + load the bootloader (PAN's own")
        print("                         sequence: reset then --loadcache)")
        print('  cmd    "<u-boot cmd>"  send a command through the mailbox')
        print("  fpga   [--bitstream F] [--addr 0xN] [--reprogram] [--force]")
        print("  ports  [--dev N]       GSER lane roles + BGX LMAC inventory")
        print("  csr    <name> [--dev N]")
        return 0 if a and a[0] in ("-h", "--help") else 2
    return CMDS[a[0]](a[1:])


if __name__ == "__main__":
    sys.exit(main())
