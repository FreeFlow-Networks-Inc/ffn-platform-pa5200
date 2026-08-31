#!/usr/bin/env python3
"""ffn_oct.py -- OCTEON complex bring-up for FFN on PA-5200 "Gryphon" hardware.

Target: reclaimed PA-5220 (see the FFN e-waste mission). Layout of that box:

    x86 Xeon-D (MP, runs FFN)
        | PCIe
        +-- CP OCTEON  (1)   <- u-boot ..._cp_pciboot.bin
        +-- DP OCTEON  (1)   <- u-boot ..._dp_pciboot.bin  (5250=2, 5260/5280=3)
        +-- FE100 front-end ASIC + FPGA (ca1/ce40 bitstreams)
            24 ports, 20 Gbps aggregate NIF bandwidth

Bring-up follows the DOCUMENTED Cavium/Marvell OCTEON remote-boot mechanism
(OCTEON SDK host `remote-lib`, board type CVMX_BOARD_TYPE_MODULE_PCIE_EP_4X --
the Octeon is a PCIe *endpoint*):

    1. slot reset            (knob hw.slotctl.<slot> = "reset", via ffn-sysd)
    2. map the endpoint BARs (vfio-pci, or sysfs resourceN)
    3. write the bootloader into Octeon DRAM through the BAR window
    4. hand u-boot a boot command
    5. load the MIPS64 dataplane kernel/app the same way
    6. load FPGA bitstreams (FFN-signed manifest)
    7. wait for the DP agent handshake, then hand off to FFN's control plane

FFN ships NO vendor code: PAN's pcic/if_pci/pci_dma_host modules are replaced by
vfio-pci + FFN's own BAR/DMA path, and the DP payload is FFN's own build.

SAFETY: every operation that writes to hardware is DRY-RUN by default. Real
writes require --force AND an explicit --pci target, because writing to the wrong
PCIe BAR can brick a device.

CLI:
    ffn_oct.py --discover                 list OCTEON endpoints + profile
    ffn_oct.py --plan [--model PA-5220]   print the exact bring-up sequence
    ffn_oct.py --bind   --pci <addr>      bind endpoint to vfio-pci   (needs --force)
    ffn_oct.py --unbind --pci <addr>      release it                  (needs --force)
    ffn_oct.py --bringup --pci <addr>     run the sequence            (needs --force)
    ffn_oct.py --selftest                 logic test (no hardware needed)
"""
import glob
import json
import os
import time
import struct
import sys

PCI_VENDOR_CAVIUM = "177d"
SYSFS_PCI = "/sys/bus/pci/devices"
VFIO_DRIVER = "vfio-pci"

# ---------------------------------------------------------------------------
# Per-model platform profiles, transcribed from the reference platform's own
# model tables (etc/cfgdb.d/octcfg + etc/cfgdb/dp/5200/<model>/). These are
# hardware FACTS about the chassis, used to drive FFN's own bring-up.
# ---------------------------------------------------------------------------
PROFILES = {
    "PA-5220": {"dp_instances": 1, "cp_instances": 1, "portcount": 24,
                "maxnifbw_kbps": 20000000,
                "fpa_pools": [(86016, 2048), (856600, 128)],
                "fe100": {"usecase": 1, "cfg_mode": 4, "v4_v6_choice": 2}},
    "PA-5250": {"dp_instances": 2, "cp_instances": 1, "portcount": 24,
                "maxnifbw_kbps": 40000000,
                "fpa_pools": [(86016, 2048), (856600, 128)],
                "fe100": {"usecase": 1, "cfg_mode": 4, "v4_v6_choice": 2}},
    "PA-5260": {"dp_instances": 3, "cp_instances": 1, "portcount": 24,
                "maxnifbw_kbps": 72000000,
                "fpa_pools": [(262144, 2048), (2097119, 128)],
                "fe100": {"usecase": 1, "cfg_mode": 4, "v4_v6_choice": 2}},
    "PA-5280": {"dp_instances": 3, "cp_instances": 1, "portcount": 24,
                "maxnifbw_kbps": 72000000,
                "fpa_pools": [(262144, 2048), (2097119, 128)],
                "fe100": {"usecase": 1, "cfg_mode": 4, "v4_v6_choice": 2}},
}
DEFAULT_MODEL = "PA-5220"

# Boot artifacts, by role. FFN stages its own equivalents here; on a reclaimed
# box the vendor images already present may be reused IN PLACE on that box.
ARTIFACT_DIR = os.environ.get("FFN_OCT_ARTIFACTS", "/var/lib/ffn-ngfw/gryphon")
ROLE_ARTIFACTS = {
    "cp": {"uboot": "u-boot-gryphon_cp_pciboot.bin"},
    "dp": {"uboot": "u-boot-gryphon_dp_pciboot.bin",
           "uboot_alt": "u-boot-gryphon_dp_etch1_pciboot.bin",
           "kernel": "vmlinux.oct2-dp"},
}
# Octeon DRAM load addresses used by the remote-boot flow. 0x100000 is the
# bootloader entry the SDK host tool defaults to ("%s 0x100000 passthrough").
LOAD_ADDR_UBOOT = 0x100000
LOAD_ADDR_KERNEL = 0x1000000

# Firmware the owner imported from their OWN media via ffn_vendor.py. It is used
# in place on this box and is never packaged into an image.
VENDOR_DIR = os.environ.get("FFN_VENDOR_DIR", "/var/lib/ffn-ngfw/vendor")


def _vendor_registry():
    try:
        with open(os.path.join(VENDOR_DIR, "registry.json")) as f:
            return json.load(f).get("artifacts", [])
    except Exception:
        return []


def resolve_artifact(fname, platform=None):
    """Find a boot artifact, preferring FFN's own staging area, then the
    owner-imported vendor firmware.

    Returns (path, source, verified). `verified` for a vendor file means it
    matched the vendor's own SHA-256 manifest at import time -- integrity, not
    authenticity (FFN cannot check Palo Alto's signature).
    """
    p = os.path.join(ARTIFACT_DIR, fname)
    if os.path.exists(p):
        return p, "ffn", False
    def _same(a, b):
        if a == b:
            return True
        # The DP kernel ships as vmlinux-<ver>-oct<N>-dp with a vmlinux.oct<N>-dp
        # symlink beside it; either spelling refers to the same image.
        return (a.startswith("vmlinux") and b.startswith("vmlinux")
                and a.endswith("-dp") and b.endswith("-dp"))

    for rec in _vendor_registry():
        f = rec.get("file", "")
        if _same(os.path.basename(f), fname) and os.path.exists(f):
            if rec.get("platform_mismatch"):
                continue          # never offer another platform's firmware
            return f, "vendor:%s" % (rec.get("platform") or "?"),                    rec.get("integrity") == "ok"
    return p, None, False

BAR_WINDOW_CHUNK = 64 * 1024        # windowed copy granularity


def _read(p, d=""):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return d


# ---------------------------------------------------------------------------

def _res_kind(idx):
    """What a /sys/.../resource line actually is.

    Linux's per-device resource array is not "BAR0..BARn": indices 0-5 are the
    six real PCI BARs, index 6 is the expansion ROM, 7-10 are bridge windows
    and 11-16 are SR-IOV BARs. Calling index 7 "BAR7" is how a 64 MB region
    that is NOT a memory window ends up looking like the biggest BAR on the
    device -- which on this appliance it does. Label it for what it is.
    """
    if idx <= 5:
        return "bar"
    if idx == 6:
        return "rom"
    if idx <= 10:
        return "bridge-window"
    return "iov"


def discover_endpoints():
    """OCTEON PCIe endpoints, with BAR geometry and current driver binding."""
    out = []
    for dev in sorted(glob.glob(SYSFS_PCI + "/*")):
        if _read(os.path.join(dev, "vendor")).replace("0x", "").lower() != PCI_VENDOR_CAVIUM:
            continue
        addr = os.path.basename(dev)
        drv = ""
        try:
            drv = os.path.basename(os.readlink(os.path.join(dev, "driver")))
        except Exception:
            pass
        bars = []
        for i, ln in enumerate(_read(os.path.join(dev, "resource")).splitlines()):
            p = ln.split()
            if len(p) < 2:
                continue
            try:
                s, e = int(p[0], 16), int(p[1], 16)
            except ValueError:
                continue
            if e > s:
                bars.append({"bar": i, "kind": _res_kind(i),
                             "start": s, "size": e - s + 1,
                             "sysfs": os.path.join(dev, "resource%d" % i)})
        out.append({"pci": addr, "device_id": _read(os.path.join(dev, "device")).replace("0x", ""),
                    "driver": drv, "bars": bars,
                    "numa_node": _read(os.path.join(dev, "numa_node"), "-1")})
    return out


def detect_model():
    """Best-effort model from DMI; falls back to the PA-5220 profile."""
    prod = _read("/sys/class/dmi/id/product_name")
    for m in PROFILES:
        if m.lower() in prod.lower():
            return m
    return DEFAULT_MODEL


def profile(model=None):
    m = model or detect_model()
    return m, PROFILES.get(m, PROFILES[DEFAULT_MODEL])


# ---------------------------------------------------------------------------
def artifact_status(model=None):
    """Which boot artifacts are staged for each Octeon role."""
    _m, prof = profile(model)
    roles = ["cp"] * prof["cp_instances"] + ["dp"] * prof["dp_instances"]
    out = []
    counters = {"cp": 0, "dp": 0}
    for role in roles:
        want = ROLE_ARTIFACTS[role]
        # instance numbering is per-role and 0-based (cp0, dp0, dp1, ...) to
        # match the platform's own naming (var.dp0, md.apps.s1.dp0).
        entry = {"role": role, "instance": counters[role], "files": {}}
        counters[role] += 1
        for kind, fname in want.items():
            p, src, ver = resolve_artifact(fname)
            present = bool(src) and os.path.exists(p)
            entry["files"][kind] = {
                "name": fname, "path": p, "present": present,
                "source": src, "verified": ver,
                "size": (os.path.getsize(p) if present else 0)}
        out.append(entry)
    return out


def verify_manifest(manifest_path=None):
    """Verify staged artifacts against an FFN-signed manifest (same HMAC scheme
    as the FIPS software-integrity manifest). Absent manifest => unverified."""
    mp = manifest_path or os.path.join(ARTIFACT_DIR, "MANIFEST.json")
    if not os.path.exists(mp):
        return {"verified": False, "reason": "no manifest at %s" % mp, "files": {}}
    import hashlib
    try:
        with open(mp) as f:
            man = json.load(f)
    except Exception as e:
        return {"verified": False, "reason": "unreadable manifest: %s" % e, "files": {}}
    res = {}
    ok = True
    for fname, want in (man.get("sha256") or {}).items():
        p = os.path.join(ARTIFACT_DIR, fname)
        if not os.path.exists(p):
            res[fname] = "missing"
            ok = False
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        good = (h.hexdigest() == want)
        res[fname] = "ok" if good else "MISMATCH"
        ok = ok and good
    return {"verified": ok, "reason": "" if ok else "hash mismatch/missing", "files": res}


# ---------------------------------------------------------------------------
def _write_sysfs(path, value):
    with open(path, "w") as f:
        f.write(value)


def bind_vfio(pci, force=False):
    """Unbind the current driver and bind the endpoint to vfio-pci."""
    dev = os.path.join(SYSFS_PCI, pci)
    if not os.path.isdir(dev):
        return False, "no such PCI device %s" % pci
    steps = []
    cur = ""
    try:
        cur = os.path.basename(os.readlink(os.path.join(dev, "driver")))
    except Exception:
        pass
    if cur == VFIO_DRIVER:
        return True, "already bound to %s" % VFIO_DRIVER
    if cur:
        steps.append(("unbind from %s" % cur, os.path.join(dev, "driver", "unbind"), pci))
    steps.append(("set driver_override=%s" % VFIO_DRIVER,
                  os.path.join(dev, "driver_override"), VFIO_DRIVER))
    steps.append(("probe", "/sys/bus/pci/drivers_probe", pci))
    if not force:
        return False, "DRY-RUN: would " + "; ".join(s[0] for s in steps)
    for desc, path, val in steps:
        try:
            _write_sysfs(path, val)
        except Exception as e:
            return False, "failed to %s: %s" % (desc, e)
    return True, "bound %s to %s" % (pci, VFIO_DRIVER)


def unbind_vfio(pci, force=False):
    dev = os.path.join(SYSFS_PCI, pci)
    if not force:
        return False, "DRY-RUN: would clear driver_override and re-probe %s" % pci
    try:
        _write_sysfs(os.path.join(dev, "driver", "unbind"), pci)
    except Exception:
        pass
    try:
        _write_sysfs(os.path.join(dev, "driver_override"), "")
        _write_sysfs("/sys/bus/pci/drivers_probe", pci)
    except Exception as e:
        return False, str(e)
    return True, "released %s" % pci


class BarWindow:
    """mmap of a PCIe BAR via sysfs resourceN -- the window used to reach Octeon
    DRAM and its registers. Read-only unless writable=True."""

    def __init__(self, sysfs_resource, size, writable=False):
        self.path = sysfs_resource
        self.size = size
        self.writable = writable
        self._f = None
        self._mm = None

    def __enter__(self):
        import mmap
        flags = os.O_RDWR if self.writable else os.O_RDONLY
        self._f = os.open(self.path, flags | getattr(os, "O_SYNC", 0))
        prot = mmap.PROT_READ | (mmap.PROT_WRITE if self.writable else 0)
        self._mm = mmap.mmap(self._f, self.size, mmap.MAP_SHARED, prot)
        return self

    def __exit__(self, *a):
        try:
            if self._mm:
                self._mm.close()
        finally:
            if self._f is not None:
                os.close(self._f)

    def read32(self, off):
        return struct.unpack("<I", self._mm[off:off + 4])[0]

    def write32(self, off, val):
        if not self.writable:
            raise PermissionError("BAR window opened read-only")
        self._mm[off:off + 4] = struct.pack("<I", val)

    def write_blob(self, off, data):
        if not self.writable:
            raise PermissionError("BAR window opened read-only")
        n = 0
        while n < len(data):
            chunk = data[n:n + BAR_WINDOW_CHUNK]
            self._mm[off + n:off + n + len(chunk)] = chunk
            n += len(chunk)
        return n


# ---------------------------------------------------------------------------
def slot_reset(slot=1, action="reset", force=False):
    """Drive the chassis slot the way the reference platform does -- through a
    state-bus knob (hw.slotctl.<slot> = reset|down) rather than poking the CPLD
    directly, so the platform layer owns the mechanism."""
    key = "hw.slotctl.%d" % slot
    if not force:
        return False, "DRY-RUN: would set %s=%s" % (key, action)
    try:
        sys.path.insert(0, "/opt/ffn-ngfw-v2")
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ffn_sysd import SysdClient
        with SysdClient() as c:
            c.set(key, action)
        return True, "%s=%s" % (key, action)
    except Exception as e:
        return False, "sysd unavailable (%s); cannot drive %s" % (str(e)[:80], key)


# ---------------------------------------------------------------------------
# Step 6 -- remote boot: tell the Octeon to start running the u-boot we placed
# in its DRAM at step 5.
#
# The chip boots from the host. Once u-boot is in DRAM the host must (a) leave a
# boot command where the bootloader looks for it, (b) mark it valid, and (c)
# release the core from reset. The bootloader acknowledges by overwriting the
# marker, which is how we know it ran rather than sat there.
#
# ENDIANNESS -- the trap in this step
# ----------------------------------
# The Octeon is big-endian MIPS64; this host is little-endian. Every word written
# through the BAR has to be byte-swapped, and BarWindow.read32/write32 are
# explicitly little-endian ("<I"). Reusing them here would write the magic in
# backwards, the bootloader would never see it, and the failure would look like
# "the chip just didn't boot" with nothing to grep for. Same class of bug as the
# ld_le32/ld_be32 mix-up in the C dataplane, which is why this file gets its own
# big-endian accessors rather than borrowing the existing ones.
#
# WHAT IS NOT KNOWN YET
# --------------------
# The handshake magic and mailbox offset are defined by the VENDOR BOOTLOADER,
# not by FFN, so they cannot be invented here. They are declared in one place
# (BootProto) and can be recovered from the vendor u-boot image itself with
# oct_probe_bootproto(). Until they are confirmed, remote_boot() runs dry and
# refuses to touch hardware. The mechanism, the byte order and the sequence are
# what the selftest pins down.
# ---------------------------------------------------------------------------


BOOT_POLL_INTERVAL = 0.05       # seconds between handshake polls
BOOT_TIMEOUT       = 10.0       # give up after this long
BOOT_CMD_MAX       = 256        # bootloader mailboxes are small; refuse longer

# Chip registers reached through the BAR0 window. These are the documented
# OCTEON block addresses; they MUST be confirmed against the chip doc or a first
# real attempt before anyone runs this with --force in anger. Named here so
# there is exactly one place to correct them.
OCT_CSR = {
    "II": {
        "name": "CIU_PP_RST",
        "pp_reset": 0x0001070000000700,
    },
    "III": {
        "name": "RST_PP_RESET",
        "pp_reset": 0x0001180006001700,   # was ...1740 (a guess);
        # pci_start_cores in the vendor loader uses ...1700
    },
}



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Step 6 -- the REAL remote-bootcmd protocol.
#
# Recovered from the vendor's own host-side loader, which shipped unstripped:
# liboct-remote_mp.so.1, octeon_remote_send_bootcmd. These are the constants
# that library actually uses, so no guessing is involved.
#
# The mailbox is three fields in the bootloader's memory:
#
#     0x6c000   u32    state / doorbell
#     0x6c004   u32    command length
#     0x6c008   bytes  command string, at most 247 bytes
#
# Write order matters: the string first, then the length, and the state word
# LAST. The state word is the doorbell, so writing it any earlier publishes a
# command the bootloader may read before the string is there.
#
# and the wait is a poll of the same state word:
#
#     read_mem32(0x6c000); if == 2 -> ready; else usleep(500000); retry
#
# So 0x6c000 is a STATE word, not a magic number:
#     2 = bootloader ready / idle, waiting for work
#     1 = a command is present for it
# and it returns to 2 once consumed. That return IS the acknowledgement.
# send_bootcmd allows 0xc8 = 200 retries at 500 ms = 100 s.
#
# TWO WRONG TURNS, RECORDED SO THEY ARE NOT REPEATED:
#  * A first version invented a magic word and mailbox offsets. The SHAPE was
#    right (write payload, publish a marker last, then wait) but the constants
#    were fiction.
#  * A second version replaced it with a CRC32-protected u-boot environment,
#    on the strength of the bootloader string "Environment passed by remote boot
#    loader has a bad CRC!". That string is real but belongs to a DIFFERENT
#    mechanism -- passing a whole environment at boot time. It is not how a
#    single boot command is delivered. build_uboot_env() is kept for that other
#    path; step 6 uses the mailbox below.
# ---------------------------------------------------------------------------

BOOTMBOX_STATE = 0x6c000      # u32: 2 = bootloader ready, 1 = command present
BOOTMBOX_LEN   = 0x6c004      # u32: length of the command
BOOTMBOX_CMD   = 0x6c008      # the command string itself
BOOTMBOX_MAXLEN = 0xf7        # 247 -- the vendor loader refuses longer
BOOT_STATE_READY = 2
BOOT_STATE_CMD_PRESENT = 1
BOOT_POLL_SEC = 0.5           # usleep(500000)
BOOT_RETRIES = 200            # 0xc8 -> 100 s


def oct_wait_for_bootloader(bar, retries=BOOT_RETRIES, now=None, sleep=None):
    """Poll the state word until the bootloader reports ready (2).

    Mirrors octeon_remote_wait_for_bootloader: read, compare to 2, else sleep
    500 ms and retry. Returns (ready, polls_used)."""
    sleep = sleep or time.sleep
    for i in range(max(1, retries)):
        if bar.read32(BOOTMBOX_STATE) == BOOT_STATE_READY:
            return True, i + 1
        sleep(BOOT_POLL_SEC)
    return False, retries


def oct_send_bootcmd(bar, cmd, gen="II", force=False, retries=BOOT_RETRIES,
                     sleep=None):
    """Deliver one boot command through the vendor mailbox protocol.

    Ordering is the safety property and it matches the vendor loader exactly:
    the string, then its length, then the flag LAST -- publishing the flag before
    the payload would let the bootloader act on a half-written command.
    """
    trace = []
    if not isinstance(cmd, str):
        cmd = str(cmd)
    raw = cmd.encode()
    if len(raw) > BOOTMBOX_MAXLEN:
        return False, ("refusing: command is %d bytes, the bootloader accepts "
                       "at most %d" % (len(raw), BOOTMBOX_MAXLEN)), trace
    if gen not in OCT_CSR:
        return False, "refusing: unknown OCTEON generation %r" % gen, trace

    # 1. the bootloader must be ready before we touch the mailbox
    ready, polls = oct_wait_for_bootloader(bar, retries, sleep=sleep)
    trace.append(("wait-ready", BOOTMBOX_STATE, polls))
    if not ready:
        return False, ("bootloader never reported ready (state word 0x%x != %d "
                       "after %d polls)" % (BOOTMBOX_STATE, BOOT_STATE_READY,
                                            polls)), trace

    # 2. payload, 3. length, 4. flag -- in that order
    bar.write_bytes(BOOTMBOX_CMD, raw)
    trace.append(("cmd", BOOTMBOX_CMD, len(raw)))
    bar.write32(BOOTMBOX_LEN, len(raw))
    trace.append(("len", BOOTMBOX_LEN, len(raw)))
    bar.write32(BOOTMBOX_STATE, BOOT_STATE_CMD_PRESENT)
    trace.append(("flag", BOOTMBOX_STATE, BOOT_STATE_CMD_PRESENT))

    if bar.dry_run or not force:
        return False, ("DRY-RUN: would send %r (%d bytes) via the mailbox at "
                       "0x%x and wait for the bootloader to consume it"
                       % (cmd, len(raw), BOOTMBOX_STATE)), trace

    # 5. the state word returning to READY is the acknowledgement
    done, polls2 = oct_wait_for_bootloader(bar, retries, sleep=sleep)
    trace.append(("wait-consumed", BOOTMBOX_STATE, polls2))
    if not done:
        return False, ("command written but the bootloader did not consume it "
                       "within %d polls (%.0f s)"
                       % (polls2, polls2 * BOOT_POLL_SEC)), trace
    return True, ("bootloader consumed %r (ready again after %d poll(s))"
                  % (cmd, polls2)), trace


# ---------------------------------------------------------------------------
# Step 7 -- load a MIPS64 image and hand it to the bootloader.
#
# Read out of the vendor's own host tools, which shipped unstripped with
# debug_info (/mnt/clones/gryphon-artifacts/octtools on the RE box):
#
#   oct-remote-load        "Usage: %s [--devnum=device_numer] <address> <file>"
#                          "  example: %s 0x100000 passthrough"
#                          "ERROR: Unable to find named block for loading image."
#                          "__tmp_load"   "Loading to address 0x%llx"
#                          "setenv fileaddr 0x%llx"  "setenv filesize 0x%llx"
#   liboct-remote_mp.so.1  octeon_remote_named_block_find(name,&addr,&size)
#                            -> cvmx_bootmem_find_named_block, then reads
#                               [0]=base, [8]=size out of the block header
#                          pci_start_cores  (core release, below)
#
# So loading an image is three moves, not one:
#   1. look up the reserved named block "__tmp_load" and write the image into it
#   2. tell the bootloader where it landed, by sending the ordinary u-boot
#      commands `setenv fileaddr` / `setenv filesize` THROUGH THE STEP-6
#      MAILBOX -- that coupling is the part that was not guessable
#   3. boot it (`bootoct <addr>`), again through the mailbox
#
# The load address is the operator's choice in the vendor tool too; its own
# example is 0x100000, which is where LOAD_ADDR_UBOOT already comes from.
# ---------------------------------------------------------------------------

NAMED_LOAD_BLOCK = "__tmp_load"   # reserved bootmem block images are loaded into
OCT_MAX_CORES = 0x30              # 48; pci_start_cores loops core 0..0x30

# CIU_PP_RST, per part, from pci_start_cores' dispatch on cvmx_get_proc_id():
#   (proc_id >> 8) in {0xd00,0xd01,0xd02,0xd03,0xd04,0xd06,0xd07,
#                      0xd90,0xd91,0xd92,0xd93,0xd94,0xd96} -> 0x1070000000700
#   anything else                                           -> 0x1010000000100
# and parts whose proc_id is above 0xd94ff additionally get 0 written to
# 0x1180006001700 first.
#
# NOTE: FFN previously *guessed* 0x...1740 for the newest parts. The vendor
# loader uses 0x...1700 -- 0x40 out, i.e. a different register entirely.
# Guessed CSR addresses are exactly how you write into the wrong thing, so
# these come from the binary now, and OCT_CSR["III"]["pp_reset"] is corrected.
CIU_PP_RST_COMMON = 0x0001070000000700
CIU_PP_RST_LEGACY = 0x0001010000000100
RST_NEWEST_EXTRA = 0x0001180006001700

_PP_RST_COMMON_FAMILIES = frozenset((
    0xD00, 0xD01, 0xD02, 0xD03, 0xD04, 0xD06, 0xD07,
    0xD90, 0xD91, 0xD92, 0xD93, 0xD94, 0xD96))


def oct_ciu_pp_rst(proc_id):
    """Which CIU_PP_RST this part uses, mirroring pci_start_cores."""
    if ((proc_id >> 8) & 0xFFFF) in _PP_RST_COMMON_FAMILIES:
        return CIU_PP_RST_COMMON
    return CIU_PP_RST_LEGACY


def oct_load_image(bar, addr, data, force=False):
    """Write an image into Octeon DRAM at `addr`. -> (ok, msg, trace)

    Deliberately does NOT claim the address was validated against __tmp_load.
    The vendor tool does check, but it resolves the bootmem descriptor at run
    time (cvmx_bootmem_init stores it in a variable; it is not a constant in
    the binary), so FFN cannot yet reproduce that lookup. The constraint is
    real, so it is reported rather than quietly dropped.
    """
    trace = []
    if not data:
        return False, "refusing: empty image", trace
    if addr <= 0:
        return False, "refusing: implausible load address 0x%x" % addr, trace

    n = 0
    while n < len(data):
        piece = data[n:n + BAR_WINDOW_CHUNK]
        bar.write_bytes(addr + n, piece)
        n += len(piece)
    trace.append(("image", addr, n))

    caveat = ("load address NOT verified to lie inside the reserved '%s' block "
              "(needs the bootmem descriptor, which the vendor resolves at run "
              "time)" % NAMED_LOAD_BLOCK)
    chunks = (n + BAR_WINDOW_CHUNK - 1) // BAR_WINDOW_CHUNK
    if bar.dry_run or not force:
        return False, ("DRY-RUN: would write %d byte(s) at 0x%x in %d chunk(s); %s"
                       % (n, addr, chunks, caveat)), trace
    return True, "wrote %d byte(s) at 0x%x; %s" % (n, addr, caveat), trace


def oct_publish_image(bar, addr, size, gen="II", force=False, sleep=None):
    """Announce the image to the bootloader the way oct-remote-load does:
    `setenv fileaddr` then `setenv filesize`, both over the step-6 mailbox."""
    trace = []
    if size <= 0:
        return False, "refusing: zero-length image", trace
    sent = []
    for cmd in ("setenv fileaddr 0x%x" % addr, "setenv filesize 0x%x" % size):
        ok, msg, tr = oct_send_bootcmd(bar, cmd, gen=gen, force=force, sleep=sleep)
        trace.extend(tr)
        if force and not bar.dry_run and not ok:
            return False, "failed sending %r: %s" % (cmd, msg), trace
        sent.append(cmd)
    if bar.dry_run or not force:
        return False, ("DRY-RUN: would publish %s" % " then ".join(sent)), trace
    return True, "published fileaddr=0x%x filesize=0x%x" % (addr, size), trace


def oct_boot_image(bar, addr, gen="II", force=False, sleep=None):
    """`bootoct <addr>` over the mailbox -- the third move."""
    return oct_send_bootcmd(bar, "bootoct 0x%x" % addr,
                            gen=gen, force=force, sleep=sleep)


def oct_start_cores(bar, coremask, proc_id=0xD9200, force=False):
    """Release cores from reset by clearing their bits in CIU_PP_RST.

    Two different confidence levels here, and they should not be blurred:

    * The register ADDRESSES and the family dispatch are read out of
      pci_start_cores -- those are the vendor's.
    * The read-modify-write is FFN's CHOICE, not something read out of the
      binary. CIU_PP_RST holds a core in reset while its bit is set, so
      clearing only the requested bits cannot disturb a core somebody meant to
      keep parked, whereas writing the whole register (what step 5 does for a
      single core) releases everything. If a later disassembly shows the vendor
      writing a computed value instead, change this -- but clearing is the
      conservative operation, so it is the safe default in the meantime.

    pci_start_cores also has a second path, for cores already out of reset and
    parked in a debug spin loop: it releases each by writing 0 to that core's
    debug-handler base, obtained from octeon_remote_debug_handler_get_base().
    FFN cannot resolve that address yet, so the path is reported as unavailable
    rather than faked.
    """
    trace = []
    if coremask <= 0:
        return False, "refusing: empty core mask", trace
    if coremask >> OCT_MAX_CORES:
        return False, ("refusing: core mask 0x%x exceeds %d cores"
                       % (coremask, OCT_MAX_CORES)), trace

    csr = oct_ciu_pp_rst(proc_id)
    if (proc_id & 0xFFFFFF) > 0xD94FF:
        bar.write64(RST_NEWEST_EXTRA, 0)
        trace.append(("rst-extra", RST_NEWEST_EXTRA, 0))

    cur = bar.read64(csr)
    new = cur & ~coremask
    bar.write64(csr, new)
    trace.append(("ciu_pp_rst", csr, new))

    if bar.dry_run or not force:
        return False, ("DRY-RUN: would clear 0x%x in CIU_PP_RST 0x%x (0x%x -> 0x%x)"
                       % (coremask, csr, cur, new)), trace
    return True, ("released core(s) 0x%x via CIU_PP_RST 0x%x" % (coremask, csr)), trace


# ---------------------------------------------------------------------------
# Step 6 -- remote boot by handing u-boot a CRC-protected ENVIRONMENT.
#
# The earlier version of this code invented a magic-word mailbox. That was
# wrong. The vendor bootloader tells us the real mechanism itself: it contains
# the string
#
#     "Environment passed by remote boot loader has a bad CRC!"
#
# so the host does not ring a doorbell -- it writes a standard u-boot
# environment block into DRAM, and u-boot validates it by CRC32. The CRC *is*
# the validity marker; there is no separate magic and no ack word. If the CRC is
# good u-boot adopts the environment, including whatever `bootcmd` we put in it.
#
# Format (standard u-boot, single-copy):
#     [ CRC32 : 4 bytes, TARGET byte order ][ key=value \0 ] ... \0  padding
# The CRC covers everything after the CRC field, for the whole environment size
# including padding -- not just the used bytes. Getting that wrong yields a
# block that looks right and is rejected, which is exactly the failure the
# bootloader's message describes.
#
# Byte order matters: the Octeon is big-endian MIPS64 and this host is
# little-endian, so the CRC field is written big-endian. Same class of bug as
# the ld_le32/ld_be32 mix-up in the C dataplane.
#
# The vendor's boot architecture, for context on what these fields carry: the
# bootloader environment points the Octeon at a 127.1.1.x point-to-point link
# to the host, NFS-mounts its root filesystem from that host over NFSv3, takes
# its console on the first serial port at 115200, and programs the FPGA with
# u-boot's own `fpga_program` command -- which is also the route for step 8.
#
# ---------------------------------------------------------------------------

UBOOT_ENV_SIZE = 0x2000          # CONFIG_ENV_SIZE; override per platform
UBOOT_ENV_ADDR = None            # DRAM offset -- must be confirmed, see EnvSpec


def build_uboot_env(pairs, size=UBOOT_ENV_SIZE, endian="big"):
    """Serialise a u-boot environment block.

    Returns the complete block, CRC first. Raises if the pairs do not fit: a
    truncated environment would fail its own CRC and be silently ignored.
    """
    import zlib
    body = bytearray()
    for k, v in pairs.items():
        if "=" in str(k) or "\x00" in str(k) or "\x00" in str(v):
            raise ValueError("illegal character in environment entry %r" % k)
        body += str(k).encode() + b"=" + str(v).encode() + b"\x00"
    body += b"\x00"                                   # empty entry terminates
    if len(body) + 4 > size:
        raise ValueError("environment is %d bytes, does not fit in %d"
                         % (len(body) + 4, size))
    data = bytes(body).ljust(size - 4, b"\x00")       # CRC covers the padding
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return struct.pack(">I" if endian == "big" else "<I", crc) + data


def parse_uboot_env(blob, endian="big"):
    """Return (crc_ok, {key: value}). Used to verify what we are about to write,
    and to read back what the bootloader has."""
    import zlib
    if len(blob) < 5:
        return False, {}
    stored = struct.unpack_from(">I" if endian == "big" else "<I", blob, 0)[0]
    data = blob[4:]
    ok = (zlib.crc32(data) & 0xFFFFFFFF) == stored
    out = {}
    for ent in data.split(b"\x00"):
        if not ent:
            break
        k, _, v = ent.partition(b"=")
        out[k.decode("ascii", "replace")] = v.decode("ascii", "replace")
    return ok, out


class EnvSpec:
    """Where the environment goes, and how big it is.

    The ADDRESS is the one thing still unconfirmed: it is agreed between the
    host tool and the bootloader, and reading the binary has not pinned it down.
    Until it is known, remote_boot refuses -- writing an environment to the wrong
    DRAM offset is a silent no-op at best.
    """

    def __init__(self, addr=None, size=UBOOT_ENV_SIZE, endian="big",
                 source="unconfirmed"):
        self.addr = addr
        self.size = size
        self.endian = endian
        self.source = source

    @property
    def confirmed(self):
        return self.addr is not None and self.source != "unconfirmed"

    def describe(self):
        if not self.confirmed:
            return ("environment address NOT confirmed -- the CRC format is "
                    "known (u-boot standard) but where the bootloader looks for "
                    "the block is not")
        return ("addr=0x%x size=0x%x %s-endian (from %s)"
                % (self.addr, self.size, self.endian, self.source))


ENV_SPEC = EnvSpec()


def oct_remote_boot(bar, spec, bootcmd, extra=None, gen="II", core=0,
                    force=False):
    """Hand u-boot an environment and release the core. Returns (ok, msg, trace).

    Deliberately does NOT claim an acknowledgement. The old version polled for a
    magic ack that does not exist in this protocol; success here means "the
    environment was written and the core released", and whether the Octeon
    actually came up is established by the DP agent handshake (step 9), not by
    this function pretending to observe it.
    """
    trace = []
    if not spec.confirmed:
        return False, "refusing: " + spec.describe(), trace
    if gen not in OCT_CSR:
        return False, "refusing: unknown OCTEON generation %r" % gen, trace

    pairs = dict(extra or {})
    pairs["bootcmd"] = bootcmd
    try:
        blob = build_uboot_env(pairs, size=spec.size, endian=spec.endian)
    except ValueError as e:
        return False, "refusing: %s" % e, trace

    # Verify our own block before writing it: a bad CRC here would be rejected
    # by the bootloader with no other symptom.
    ok, back = parse_uboot_env(blob, endian=spec.endian)
    if not ok:
        return False, "refusing: the environment we built fails its own CRC", trace
    if back.get("bootcmd") != bootcmd:
        return False, "refusing: environment did not round-trip", trace
    trace.append(("env-verified", spec.addr, len(blob)))

    csr = OCT_CSR[gen]
    bar.write_bytes(spec.addr, blob)
    trace.append(("env", spec.addr, len(blob)))
    bar.write64(csr["pp_reset"], 0)
    trace.append(("pp_reset", csr["pp_reset"], 0))

    if bar.dry_run or not force:
        return False, ("DRY-RUN: would write a %d-byte CRC'd environment at "
                       "0x%x (%d entries, bootcmd=%r) then release core %d via "
                       "%s" % (len(blob), spec.addr, len(pairs), bootcmd, core,
                               csr["name"])), trace

    return True, ("environment written and core %d released via %s; whether the "
                  "Octeon came up is confirmed by the DP handshake, not here"
                  % (core, csr["name"])), trace




def be64(val):
    """Pack a 64-bit word big-endian, as the Octeon reads it."""
    return struct.pack(">Q", val & 0xFFFFFFFFFFFFFFFF)


def un_be64(raw):
    return struct.unpack(">Q", raw)[0]


class BootBar:
    """Big-endian view of a BAR window, with a dry-run mode.

    Wraps anything exposing __getitem__/__setitem__ over bytes (the real
    BarWindow's mmap, or a bytearray in the tests). In dry-run nothing is
    written: the intended writes are recorded so a caller -- or a test -- can
    inspect exactly what would have happened.
    """

    def __init__(self, mem, dry_run=True):
        self.mem = mem
        self.dry_run = dry_run
        self.writes = []            # [(offset, bytes)] in order

    def read64(self, off):
        return un_be64(bytes(self.mem[off:off + 8]))

    def read32(self, off):
        return struct.unpack(">I", bytes(self.mem[off:off + 4]))[0]

    def write32(self, off, val):
        raw = struct.pack(">I", val & 0xFFFFFFFF)
        self.writes.append((off, raw))
        if not self.dry_run:
            self.mem[off:off + 4] = raw

    def write64(self, off, val):
        raw = be64(val)
        self.writes.append((off, raw))
        if not self.dry_run:
            self.mem[off:off + 8] = raw

    def write_bytes(self, off, data):
        self.writes.append((off, bytes(data)))
        if not self.dry_run:
            self.mem[off:off + len(data)] = data


def oct_probe_bootproto(path, window=None):
    """Recover the handshake contract from a vendor u-boot image.

    The magic is the bootloader's, so the only honest way to learn it is to read
    the bootloader. This reports CANDIDATES -- 8-byte aligned, fully printable
    ASCII words, which is what such magics almost always are -- ranked by how
    often they appear. It deliberately does not pick one: a human confirms, and
    the chosen value goes into BootProto with source set accordingly.
    """
    try:
        with open(path, "rb") as f:
            blob = f.read() if window is None else f.read(window)
    except Exception as e:
        return {"ok": False, "error": str(e), "candidates": []}

    def printable(w):
        return all(0x20 <= b < 0x7F for b in w)

    counts = {}
    for off in range(0, len(blob) - 8, 8):
        w = blob[off:off + 8]
        if printable(w) and not w.isspace():
            counts.setdefault(w, []).append(off)

    cands = sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:12]
    out = [{"ascii": w.decode("ascii", "replace"),
            "hex": "0x%016x" % un_be64(w),
            "occurrences": len(offs),
            "first_offset": offs[0]} for w, offs in cands]

    # Strings that hint at the boot mailbox, for a human to read alongside.
    hints = []
    for needle in (b"bootcmd", b"bootloader", b"octeon", b"pciboot", b"remote"):
        i = blob.find(needle)
        if i >= 0:
            hints.append({"string": needle.decode(), "offset": i})

    return {"ok": True, "size": len(blob), "candidates": out, "hints": hints,
            "note": "candidates only -- confirm against the bootloader before use"}


def bringup_plan(model=None, pci=None):
    """The exact ordered sequence, with per-step readiness. Pure planning."""
    m, prof = profile(model)
    eps = discover_endpoints()
    target = None
    if pci:
        target = next((e for e in eps if e["pci"] == pci), None)
    elif eps:
        target = eps[0]
    arts = artifact_status(m)
    man = verify_manifest()

    steps = []

    def add(n, desc, ready, detail):
        steps.append({"step": n, "action": desc, "ready": bool(ready), "detail": detail})

    add(1, "slot reset (hw.slotctl.<slot>=reset via ffn-sysd)",
        os.path.exists("/run/ffn-ngfw/sysd.sock"),
        "sysd socket %s" % ("present" if os.path.exists("/run/ffn-ngfw/sysd.sock")
                            else "MISSING - start ffn-sysd"))
    add(2, "bind OCTEON endpoint to vfio-pci", bool(target),
        ("target %s (driver=%s)" % (target["pci"], target["driver"] or "none"))
        if target else "no OCTEON endpoint found on this host")
    add(3, "map BAR window to Octeon DRAM",
        bool(target and target["bars"]),
        (", ".join("BAR%d=%dMB" % (b["bar"], b["size"] // (1 << 20))
                   for b in target["bars"])) if target and target["bars"]
        else "no BARs enumerated")
    need = [f for a in arts for k, f in a["files"].items() if not f["present"]]
    srcs = sorted({f["source"] for a in arts for f in a["files"].values()
                   if f.get("source")})
    vend = [f for a in arts for f in a["files"].values()
            if (f.get("source") or "").startswith("vendor")]
    # Owner-supplied vendor firmware counts as staged once it is all present.
    # Note the asymmetry: the vendor's fpga-images manifest covers BITSTREAMS
    # only, so u-boot and the DP kernel have no manifest to check against --
    # requiring coverage for them would be unsatisfiable. Anything actually
    # imported is either manifest-verified or was refused at import, so
    # presence here already implies "not known-corrupt".
    nver = sum(1 for f in vend if f["verified"])
    staged_ok = (not need) and (man["verified"] or bool(vend))
    add(4, "stage + verify boot artifacts",
        staged_ok,
        ("all staged from %s; %s" % (", ".join(srcs) or "?",
         "FFN manifest verified" if man["verified"]
         else ("%d/%d vendor file(s) matched the vendor manifest (u-boot and the "
               "DP kernel have no vendor manifest to check)" % (nver, len(vend))
               if vend else man["reason"])))
        if not need else "missing: %s" % ", ".join(sorted({f["name"] for f in need})))
    add(5, "write u-boot to Octeon DRAM @0x%x (windowed copy)" % LOAD_ADDR_UBOOT,
        bool(target and target["bars"]) and not need, "per role: %s" %
        ", ".join("%s%d" % (a["role"], a["instance"]) for a in arts))
    add(6, "send the boot command through the vendor mailbox at 0x%x"
        % BOOTMBOX_STATE, True,
        ("protocol CONFIRMED from the vendor loader "
         "(liboct-remote_mp.so.1:octeon_remote_send_bootcmd, unstripped): "
         "state 0x%x (2=ready/1=present), len 0x%x, cmd 0x%x, max %d bytes, "
         "poll %.1fs x%d. Wait-ready, then string, length, flag last, then wait "
         "for it to return to ready."
         % (BOOTMBOX_STATE, BOOTMBOX_LEN, BOOTMBOX_CMD, BOOTMBOX_MAXLEN,
            BOOT_POLL_SEC, BOOT_RETRIES)))
    # This platform is OCTEON II: every vendor dataplane package for .5200 is
    # oct2 and no oct3 package exists. The "CN73XX" in lspci is a pci.ids label,
    # not the chip speaking. FFN's OCTEON-III backend stays available for
    # CN78XX-family platforms.
    _gen = "OCTEON-II (IPD/POW/PKO)"
    # Step 7's mechanism is no longer guesswork: oct-remote-load and
    # liboct-remote_mp.so.1 spell it out. Readiness is gated the same way as
    # step 5 -- there must be something staged to load.
    add(7, "load a MIPS64 image into '%s', publish fileaddr/filesize, boot it"
        % NAMED_LOAD_BLOCK,
        bool(target and target["bars"]) and not need,
        ("mechanism CONFIRMED from oct-remote-load + liboct-remote_mp.so.1: "
         "write the image into the reserved '%s' block, then send `setenv "
         "fileaddr` and `setenv filesize` through the step-6 mailbox, then "
         "`bootoct`. Core release via CIU_PP_RST 0x%x (legacy parts 0x%x). "
         "Two caveats, stated rather than hidden: the load address is not "
         "checked against the named block (the descriptor is resolved at run "
         "time, not a constant), and the debug-spin release path needs a "
         "per-core debug-handler base FFN cannot yet resolve. FFN's own %s DP "
         "is a bare-metal CVMX app (cvmx_user_app_init), so it needs NO NFS "
         "root -- load + bootoct is the whole path; the vendor's Linux DP "
         "kernel is the one that NFS-mounts /opt/dpfs. The MIPS64 big-endian "
         "cross-build exists and passes under qemu-mips64; the only thing still "
         "missing is the CVMX chip headers, and without them the backend "
         "refuses to init rather than pretending."
         % (NAMED_LOAD_BLOCK, CIU_PP_RST_COMMON, CIU_PP_RST_LEGACY, _gen)))
    # Step 8 is a command to the CP Octeon's u-boot, not a host operation, so
    # it needs a bitstream staged AND the CP bootloader already running.
    _bs = fpga_artifacts()
    _ce40 = [b for b in _bs if b["name"].startswith("ce40")]
    add(8, "program the FPGA via u-boot `%s load=none` (%d bitstream(s) staged)"
        % (FPGA_CMD, len(_bs)),
        bool(_ce40) and bool(target and target["bars"]),
        ("read from u-boot-gryphon_cp_pciboot.bin: the command exists only in "
         "the CP bootloader, so this is a mailbox command to the CP Octeon and "
         "depends on steps 5-7 having booted it. load=none programs from "
         "whatever is already in DRAM at addr= for size= bytes, so FFN needs "
         "no TFTP, NFS or target filesystem -- it writes the bitstream through "
         "the BAR window and sends one command. Defaults addr=0x%x delay=%d; "
         "the bootloader retries %d times, 1 s apart, and prints SUCCESS or "
         "FAILURE. Staged: %s. FE100 cfg %s still has no confirmed programming "
         "path. ca1.bin is a COMPRESSED full bitstream for the same KU095 part "
         "(25053 MFWR writes vs ce40's uncompressed single FDRI), sitting "
         "behind %s; nothing in either bootloader, the DP kernel, or the host "
         "PCI bus loads it, so it needs CP-side software (brdagent/libfpga.so, "
         "not imported). FFN will not guess that CPLD's register map -- use "
         "--cpld to dump it first. ca1.bin has no option in this build's "
         "parser (only ce40=), "
         "so how the second FPGA is loaded is still unknown (see below)."
         % (FPGA_DEFAULT_ADDR, FPGA_DEFAULT_DELAY_US, FPGA_ATTEMPTS,
            ", ".join("%s (%d MB, %s)"
                      % (b["name"], b["size"] // (1 << 20), b["integrity"])
                      for b in _bs) or "none",
            json.dumps(prof["fe100"]), CPLD_MAIN["node"])))
    add(9, "await DP agent handshake, hand off to FFN control plane", False,
        "message rings + doorbell; %d DP instance(s) for %s"
        % (prof["dp_instances"], m))

    ready = sum(1 for s in steps if s["ready"])
    return {"model": m, "profile": prof, "target": target,
            "endpoints": eps, "artifacts": arts, "manifest": man,
            "steps": steps, "ready_steps": ready, "total_steps": len(steps)}




# ---------------------------------------------------------------------------
# Step 8 -- program the FPGA, via u-boot's own fpga_program command.
#
# Read out of u-boot-gryphon_cp_pciboot.bin. Note WHERE it was found: the
# command exists ONLY in the CP bootloader, not the DP one. So FPGA programming
# is not a host-side operation at all -- it is a command sent to the control
# plane Octeon's u-boot, which means steps 5-7 must have booted the CP u-boot
# first. That dependency is the reason step 8 comes after step 7.
#
# The command table entry (file 0xba93c, 32-BIT pointers -- name+0, maxargs+4,
# repeatable+8, cmd+12, usage+16, help+20) gives maxargs = 10 and the handler
# at 0xc008ea94. That handler is a thin retry wrapper: it calls the real worker
# at 0xc008e6b4 up to 3 times with a 1 s (0xf4240 us) delay between attempts,
# then prints "Full fpga programming SUCCESS" or "Full fpga programming
# FAILURE, %d of %d attempts!".
#
# Its own help text, verbatim:
#
#   fpga_program - program  the fpgas
#       options:  [load=<fat|ext2|tftpboot|jffs|none>]
#                 [ce40=<image>]
#                 [addr=<load-addr>] [size=<byte-size>]
#                 [ide=<ide number>] [delay=<microsec>]
#                 [force]
#       defaults: load=tftpboot addr=400000 size=-1
#                 ide=0 delay=10000 ce40=ce40-file (no force)
#       size:     only necessary if load=none
#
# The option parser is at 0xc008e094; each option is a strlen + strncmp, and
# the values are decoded with simple_strtoul at these bases:
#
#   ce40=  0xaf570  ->  filename pointer (only used by the fetching methods)
#   addr=  0xaf578  ->  base 16, default 0x400000
#   size=  0xaf580  ->  base 16, default -1
#   ide=   0xaf588  ->  base 10, default 0
#   load=  0xaf590  ->  method enum, default 1
#   delay= 0xaf640  ->  base 16, default 10000
#   force  0xaf648  ->  flag; the env var "fpga-force" (parsed base 16) also
#                       forces, otherwise "%s FPGA is already programmed,
#                       skipping w/o force flag..."
#
# THE KEY FINDING: load=none. At 0xc008e27c the parser loads "none"
# (0xc009aab0) and strcmp's the load= value against it; on a match it writes 0
# to the method word. Method 0 fetches NOTHING -- it programs from whatever is
# already in DRAM at addr, for size bytes. So FFN needs no TFTP server, no NFS,
# and no filesystem on the target: write the bitstream through the BAR window
# (the step-7 mechanism) and send one mailbox command. That is why FFN defaults
# to load=none rather than copying the vendor's load=tftpboot.
#
# Two of the advertised methods are compiled out of this build and print an
# error rather than working: jffs ("No JFFS configure on the system") and sata
# ("No SATA configure on the system"). Anything unrecognised gets "Only
# <fat|tftpboot> are valid for load=".
#
# Also present, and NOT used by FFN: a stale help string that still advertises
# option names belonging to two other vendor platforms. The option this build
# actually parses is ce40=. For the same reason FFN does not copy the vendor's
# own default fpga env line -- it sets a numeric option that is not in this
# build's option table at all, so that default is stale for gryphon.
# ---------------------------------------------------------------------------

FPGA_CMD = "fpga_program"

# The method enum, exactly as the parser assigns it. jffs/sata are listed
# because the help advertises them, with the reason they cannot be used.
FPGA_LOAD_METHODS = {"none": 0, "tftpboot": 1, "fat": 2, "ext2": 3}
FPGA_METHODS_COMPILED_OUT = {
    "jffs": "No JFFS configure on the system",
    "sata": "No SATA configure on the system"}

FPGA_DEFAULT_ADDR = 0x400000        # the vendor's own default
FPGA_DEFAULT_DELAY_US = 10000
FPGA_DEFAULT_IDE = 0
FPGA_MAXARGS = 10                   # from the command table entry
FPGA_ATTEMPTS = 3                   # the wrapper's retry count
FPGA_RETRY_DELAY_US = 1000000       # 0xf4240, the wrapper's inter-attempt wait
FPGA_FORCE_ENV = "fpga-force"

# The mailbox lives here, so a bitstream written over it would destroy the very
# channel used to announce it. Span = state + len + the biggest command.
_MBOX_LO = BOOTMBOX_STATE
_MBOX_HI = BOOTMBOX_CMD + BOOTMBOX_MAXLEN


def build_fpga_program_cmd(addr=FPGA_DEFAULT_ADDR, size=None, force=False,
                           method="none", image=None, delay=None, ide=None):
    """Build an fpga_program command line. Raises ValueError on anything the
    bootloader would reject or silently misread.

    Hex values are emitted bare, no 0x: the parser calls simple_strtoul with an
    explicit base, and bare hex is the form the vendor's own defaults document
    ("addr=400000").
    """
    if method in FPGA_METHODS_COMPILED_OUT:
        raise ValueError("load=%s is advertised but compiled out of this "
                         "bootloader: %s"
                         % (method, FPGA_METHODS_COMPILED_OUT[method]))
    if method not in FPGA_LOAD_METHODS:
        raise ValueError("unknown load method %r; this build parses %s"
                         % (method, "|".join(sorted(FPGA_LOAD_METHODS))))
    if addr is None or addr <= 0:
        raise ValueError("implausible load address %r" % addr)

    parts = [FPGA_CMD, "load=%s" % method]
    if method == "none":
        # "size: only necessary if load=none" -- and it is genuinely necessary,
        # because the default of -1 means "however much the fetch returned" and
        # nothing was fetched.
        if not size or size <= 0:
            raise ValueError("load=none programs from DRAM, so size= is "
                             "required and must be positive")
    if image is not None:
        if method == "none":
            raise ValueError("ce40= names a file to fetch; it is meaningless "
                             "with load=none")
        parts.append("ce40=%s" % image)
    parts.append("addr=%x" % addr)
    if size is not None and size > 0:
        parts.append("size=%x" % size)
    if ide is not None:
        parts.append("ide=%d" % ide)          # base 10, unlike the others
    if delay is not None:
        parts.append("delay=%x" % delay)
    if force:
        parts.append("force")

    # maxargs counts argv[0], so at most FPGA_MAXARGS tokens total.
    if len(parts) > FPGA_MAXARGS:
        raise ValueError("%d tokens exceeds the command's maxargs of %d"
                         % (len(parts), FPGA_MAXARGS))
    cmd = " ".join(parts)
    if len(cmd) > BOOTMBOX_MAXLEN:
        raise ValueError("command is %d bytes, over the mailbox limit of %d"
                         % (len(cmd), BOOTMBOX_MAXLEN))
    return cmd


def fpga_region_conflict(addr, size):
    """Does [addr, addr+size) collide with the boot mailbox? Returns a reason
    string, or None if the region is clear."""
    if addr < _MBOX_HI and (addr + size) > _MBOX_LO:
        return ("the image would span the boot mailbox at 0x%x-0x%x, which is "
                "the channel used to announce it" % (_MBOX_LO, _MBOX_HI))
    return None


def oct_program_fpga(bar, data, addr=FPGA_DEFAULT_ADDR, force_program=False,
                     gen="II", force=False, sleep=None, delay=None):
    """Step 8: write a bitstream into Octeon DRAM, then have the CP u-boot
    program it from there. -> (ok, msg, trace)

    `force_program` is the bootloader's `force` option (reprogram an
    already-programmed FPGA). `force` is FFN's usual "actually touch hardware"
    gate -- two different things, deliberately not merged.
    """
    trace = []
    if not data:
        return False, "refusing: empty bitstream", trace

    bad = fpga_region_conflict(addr, len(data))
    if bad:
        return False, "refusing: %s" % bad, trace

    try:
        cmd = build_fpga_program_cmd(addr=addr, size=len(data),
                                     force=force_program, method="none",
                                     delay=delay)
    except ValueError as e:
        return False, "refusing: %s" % e, trace

    ok, msg, tr = oct_load_image(bar, addr, data, force=force)
    trace.extend(tr)
    if force and not bar.dry_run and not ok:
        return False, "writing the bitstream failed: %s" % msg, trace

    ok, msg, tr = oct_send_bootcmd(bar, cmd, gen=gen, force=force, sleep=sleep)
    trace.extend(tr)

    if bar.dry_run or not force:
        return False, ("DRY-RUN: would write %d bytes at 0x%x then send %r"
                       % (len(data), addr, cmd)), trace
    if not ok:
        return False, "sending %r failed: %s" % (cmd, msg), trace
    return True, ("bitstream staged at 0x%x and %r sent; the bootloader retries "
                  "up to %d times and reports SUCCESS/FAILURE on its console -- "
                  "that console line, not this write, is the confirmation"
                  % (addr, cmd, FPGA_ATTEMPTS)), trace


def fpga_artifacts(platform=None):
    """Owner-imported bitstreams, from the vendor registry."""
    out = []
    for rec in _vendor_registry():
        if rec.get("kind") != "fpga" or rec.get("platform_mismatch"):
            continue
        f = rec.get("file", "")
        if not os.path.exists(f):
            continue
        out.append({"name": os.path.basename(f), "file": f,
                    "size": rec.get("size") or 0,
                    "integrity": rec.get("integrity") or "unchecked"})
    return sorted(out, key=lambda r: r["name"])


# ---------------------------------------------------------------------------
# The two CPLDs, the two FPGAs, and why FFN cannot program the second one yet.
#
# The CP bootloader's own embedded device tree (FDT at file 0x8f010,
# model "pan,pa-5200") puts two CPLDs on the Octeon boot bus:
#
#   pan-cpld@2,0     compatible "gryphon-cp,cpld"      chip select 2
#   pan-ce-cpld@3,0  compatible "gryphon-cp,ce-cpld"   chip select 3
#
# There is NO fpga node anywhere in that device tree -- the FPGAs are not PCI
# or DT devices, they hang off these CPLDs. u-boot exposes each CPLD's
# registers 0-0xa through its own command (`cpld_reg` and `ce_cpld_reg`).
#
# fpga_program streams through the CE CPLD only, and the protocol is visible in
# the worker's programming loop (0xc008e98c-0xc008e9f0):
#
#   1. write 5 to CE CPLD register 2   (control: begin configuration)
#   2. udelay(delay)                   (the delay= option, default 10000)
#   3. for each byte of the image: write it to CE CPLD register 3
#      (progress is printed every 1000000 bytes; there is no per-byte delay)
#   4. write 0 to CE CPLD register 2   (control: end)
#   5. a done-sequence check follows; failure retries the whole thing, 3 times
#
# The CE CPLD base is hardcoded (the global at 0xc00c104c) -- u-boot never
# streams through the other CPLD -- and a version check on it prints
# "CE CPLD version check failure" when it reads 0.
#
# WHERE ca1.bin FITS -- and what is still unproven.
#
# Both images are Xilinx Kintex UltraScale KU095 (IDCODE 0x0390d093, one IDCODE
# write each), so they are full-device configurations for the same part, not a
# base plus a partial overlay:
#
#   ce40.bin  46.02 MiB  uncompressed (1 FDRI write, 2 type-2 payloads)
#   ca1.bin   23.50 MiB  COMPRESSED (25053 MFWR writes, 25995 FAR) -- which is
#                        why it is about half the size despite the same part
#
# Everything that could load ca1.bin has been checked and ruled out:
#
#   CP u-boot     only knows "ce40"; "ca1" appears nowhere in it, and its sole
#                 FPGA command streams through the CE CPLD. The one other name
#                 string the code can print is "unknown" (0x91940), a fallback
#                 label, not a second device.
#   DP u-boot     no FPGA code at all (both dp and dp_etch1)
#   DP kernel     vmlinux-3.10.87-oct2-dp has no FPGA symbols; only i2c mux
#                 CPLD reset handling
#   host PCI      no Xilinx (10ee) device on the bus, so neither FPGA is
#                 host-programmable over PCIe
#   fpga-images   the vendor manifest carries hashes only, no device or load
#                 order information
#
# So ca1.bin is programmed by CP-side software rather than by any bootloader.
# The candidate is brdagent with brdagent/cp/libfpga.so, which was seen on the
# Gryphon drive but is NOT among the imported artifacts -- so this is a
# well-supported inference, NOT a confirmed mechanism. The likely shape is the
# same byte-stream, through pan-cpld (chip select 2) instead of the CE CPLD.
#
# FFN therefore does NOT program ca1. Guessing which of pan-cpld's 11 registers
# is control and which is data, and writing to them, is exactly the kind of
# blind poke that damages hardware. What FFN offers instead is the READ-ONLY
# probe below, so an operator on real hardware can dump both CPLDs and confirm
# the register map first.
# ---------------------------------------------------------------------------

CPLD_MAIN = {"node": "pan-cpld@2,0", "compatible": "gryphon-cp,cpld",
             "cs": 2, "cmd": "cpld_reg", "fpga": "ca1 (presumed)"}
CPLD_CE = {"node": "pan-ce-cpld@3,0", "compatible": "gryphon-cp,ce-cpld",
           "cs": 3, "cmd": "ce_cpld_reg", "fpga": "ce40"}
CPLDS = {"main": CPLD_MAIN, "ce": CPLD_CE}

CPLD_REG_MAX = 0xA                  # both commands document "[0-a]"

# The CE CPLD programming protocol, read out of the worker. Recorded because it
# is the template FFN would follow for the second CPLD once its map is known --
# NOT because FFN currently drives it directly (u-boot does).
CE_CPLD_CTRL_REG = 2
CE_CPLD_DATA_REG = 3
CE_CPLD_CTRL_BEGIN = 5
CE_CPLD_CTRL_END = 0

FPGA_IDCODE_KU095 = 0x0390D093


def cpld_cmd(which, action="display", reg=None, value=None):
    """A u-boot cpld_reg / ce_cpld_reg command line.

    `display` and `read` are read-only. `write` is built only when explicitly
    asked for, because a blind write to an unknown CPLD register is how boards
    get damaged -- callers that program hardware must justify it themselves.
    """
    if which not in CPLDS:
        raise ValueError("unknown CPLD %r; known: %s"
                         % (which, "|".join(sorted(CPLDS))))
    if action not in ("display", "read", "write"):
        raise ValueError("action must be display, read or write, got %r" % action)
    base = CPLDS[which]["cmd"]
    if action == "display":
        return "%s display" % base
    if reg is None or not (0 <= reg <= CPLD_REG_MAX):
        raise ValueError("register must be 0-0x%x, got %r" % (CPLD_REG_MAX, reg))
    if action == "read":
        return "%s read %x" % (base, reg)
    if action == "write":
        if value is None or not (0 <= value <= 0xFF):
            raise ValueError("write needs a byte value, got %r" % value)
        return "%s write %x %x" % (base, reg, value)
    raise AssertionError("unreachable: action validated above")


def oct_cpld_probe(bar, which="ce", gen="II", force=False, sleep=None):
    """Read-only: ask the CP bootloader to dump one CPLD's registers.

    The answer comes back on the Octeon's serial console, not through the
    mailbox -- the mailbox carries commands, and nothing reads a reply. So this
    schedules the dump; the operator reads it on the console.
    """
    try:
        cmd = cpld_cmd(which, "display")
    except ValueError as e:
        return False, "refusing: %s" % e, []
    ok, msg, tr = oct_send_bootcmd(bar, cmd, gen=gen, force=force, sleep=sleep)
    if not ok:
        return ok, msg, tr
    return True, ("sent %r; the register dump appears on the Octeon console -- "
                  "compare %s against the CE CPLD before trusting any register "
                  "map for it" % (cmd, CPLDS[which]["node"])), tr


def identify_bitstream(path, limit=8192):
    """Read a raw Xilinx .bin header: which part, and is it compressed.

    Pure parsing of the public bitstream packet format -- no vendor code and no
    vendor tables involved.
    """
    try:
        blob = open(path, "rb").read(limit)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    i = blob.find(b"\xaa\x99\x55\x66")
    if i < 0:
        return {"ok": False, "error": "no Xilinx sync word; not a raw bitstream"}
    i += 4
    idcode = None
    while i + 4 <= len(blob):
        w = struct.unpack(">I", blob[i:i + 4])[0]
        i += 4
        if (w >> 29) != 1:
            if (w >> 29) == 2:
                break
            continue
        op = (w >> 27) & 3
        reg = (w >> 13) & 0x3FFF
        cnt = w & 0x7FF
        if op == 2 and cnt:
            if reg == 12:
                idcode = struct.unpack(">I", blob[i:i + 4])[0]
                break
            i += 4 * cnt
    out = {"ok": idcode is not None, "idcode": idcode,
           "part": "KU095" if idcode == FPGA_IDCODE_KU095 else "unknown",
           "size": os.path.getsize(path) if os.path.exists(path) else 0}
    if idcode is None:
        out["error"] = "no IDCODE write in the first %d bytes" % limit
    return out

def _configured_octeon_gen():
    """The operator's/profile's decision, written to /etc/ffn-ngfw/octeon-gen by
    provisioning. Defaults to II: every PA-5200 dataplane package the vendor
    ships is oct2, so II is the right default to be wrong towards."""
    try:
        v = open("/etc/ffn-ngfw/octeon-gen").read().strip()
    except Exception:
        return "II"
    return "III" if v == "3" else "II"


def load_and_boot(pci=None, path=None, addr=LOAD_ADDR_UBOOT, boot=False,
                  gen=None, force=False, bar_idx=None):
    """Step 7 end to end over a real BAR: write the image, publish
    fileaddr/filesize through the mailbox, optionally bootoct.

    Scope note, so the limit is visible rather than discovered the hard way:
    the image window AND the mailbox both live inside the DRAM BAR, so this
    path is complete. Core release does NOT -- CIU_PP_RST is an Octeon physical
    CSR address, not a BAR offset, and reaching it needs the endpoint's CSR
    window. So oct_start_cores() is deliberately not called from here.
    """
    if not path or not os.path.exists(path):
        return {"ok": False, "error": "no such image: %r" % path}
    eps = discover_endpoints()
    target = None
    for e in eps:
        if pci is None or e["pci"] == pci:
            target = e
            break
    if not target:
        return {"ok": False, "error": "no OCTEON endpoint %s" % (pci or "")}
    if not target["bars"]:
        return {"ok": False, "error": "endpoint %s has no BARs" % target["pci"]}

    # Which window is Octeon DRAM is NOT decidable by size, and guessing it
    # wrong means writing an image over a register window. This appliance is the
    # proof: BAR2 and resource index 7 are both 64 MB, and index 7 is not even a
    # BAR. So require the operator to say, and refuse with the candidates
    # listed rather than picking one.
    real = [b for b in target["bars"] if b["kind"] == "bar"]
    if bar_idx is None:
        return {"ok": False,
                "error": "which BAR is Octeon DRAM is not inferable; pass "
                         "--bar N",
                "candidates": [{"bar": b["bar"], "size_mb": b["size"] // (1 << 20),
                                "kind": b["kind"]} for b in target["bars"]]}
    sel = [b for b in real if b["bar"] == bar_idx]
    if not sel:
        return {"ok": False,
                "error": "resource %d is not a memory BAR on %s"
                         % (bar_idx, target["pci"]),
                "candidates": [{"bar": b["bar"], "size_mb": b["size"] // (1 << 20)}
                               for b in real]}
    bar = sel[0]
    try:
        data = open(path, "rb").read()
    except Exception as e:
        return {"ok": False, "error": "cannot read %s: %s" % (path, e)}

    need = addr + len(data)
    if need > bar["size"]:
        return {"ok": False, "error":
                "image needs 0x%x bytes at 0x%x but BAR%d is only 0x%x"
                % (len(data), addr, bar["bar"], bar["size"])}

    g = gen or _configured_octeon_gen()
    out = {"pci": target["pci"], "bar": bar["bar"], "image": path,
           "size": len(data), "addr": addr, "gen": g, "steps": []}

    if not force:
        out["ok"] = False
        out["dry_run"] = True
        out["error"] = "refusing to write to hardware without --force"
        return out

    with BarWindow(bar["sysfs"], bar["size"], writable=True) as win:
        bb = BootBar(win._mm, dry_run=False)
        ok, msg, _ = oct_load_image(bb, addr, data, force=True)
        out["steps"].append({"load": msg, "ok": ok})
        if not ok:
            out["ok"] = False
            return out
        ok, msg, _ = oct_publish_image(bb, addr, len(data), gen=g, force=True)
        out["steps"].append({"publish": msg, "ok": ok})
        if not ok:
            out["ok"] = False
            return out
        if boot:
            ok, msg, _ = oct_boot_image(bb, addr, gen=g, force=True)
            out["steps"].append({"boot": msg, "ok": ok})

    out["ok"] = ok
    out["note"] = ("whether the Octeon actually came up is confirmed by the DP "
                   "handshake (step 9), not by these writes succeeding")
    return out


def program_fpga(pci=None, path=None, addr=FPGA_DEFAULT_ADDR, bar_idx=None,
                 force_program=False, gen=None, force=False):
    """Step 8 over a real BAR: stage the bitstream, then tell the CP u-boot to
    program from DRAM. Same BAR caveat as load_and_boot -- the operator names
    the window, because guessing it wrong means writing tens of MB into
    registers."""
    if path is None:
        bs = [b for b in fpga_artifacts() if b["name"].startswith("ce40")]
        if len(bs) != 1:
            return {"ok": False,
                    "error": "name the bitstream with --fpga <file>",
                    "staged": fpga_artifacts()}
        path = bs[0]["file"]
    if not os.path.exists(path):
        return {"ok": False, "error": "no such bitstream: %r" % path}

    # fpga_program streams through the CE CPLD and knows only "ce40". Handing
    # it the other FPGA's image would push ca1's bitstream into the ce40 device.
    if not os.path.basename(path).startswith("ce40"):
        return {"ok": False, "error":
                "%s is not a ce40 image; u-boot's fpga_program streams through "
                "the CE CPLD only. The second FPGA (ca1) is behind %s and no "
                "bootloader loads it -- see the CPLD notes in this module."
                % (os.path.basename(path), CPLD_MAIN["node"]),
                "bitstream": identify_bitstream(path)}

    eps = discover_endpoints()
    target = None
    for e in eps:
        if pci is None or e["pci"] == pci:
            target = e
            break
    if not target:
        return {"ok": False, "error": "no OCTEON endpoint %s" % (pci or "")}

    real = [b for b in target["bars"] if b["kind"] == "bar"]
    if bar_idx is None:
        return {"ok": False,
                "error": "which BAR is Octeon DRAM is not inferable; pass --bar N",
                "candidates": [{"bar": b["bar"], "size_mb": b["size"] // (1 << 20),
                                "kind": b["kind"]} for b in target["bars"]]}
    sel = [b for b in real if b["bar"] == bar_idx]
    if not sel:
        return {"ok": False,
                "error": "resource %d is not a memory BAR on %s"
                         % (bar_idx, target["pci"])}
    bar = sel[0]

    try:
        data = open(path, "rb").read()
    except Exception as e:
        return {"ok": False, "error": "cannot read %s: %s" % (path, e)}

    if addr + len(data) > bar["size"]:
        return {"ok": False, "error":
                "bitstream needs 0x%x bytes at 0x%x but BAR%d is only 0x%x"
                % (len(data), addr, bar["bar"], bar["size"])}

    g = gen or _configured_octeon_gen()
    out = {"pci": target["pci"], "bar": bar["bar"], "bitstream": path,
           "size": len(data), "addr": addr, "gen": g}
    try:
        out["command"] = build_fpga_program_cmd(addr=addr, size=len(data),
                                                force=force_program)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if not force:
        out["ok"] = False
        out["dry_run"] = True
        out["error"] = "refusing to write to hardware without --force"
        return out

    with BarWindow(bar["sysfs"], bar["size"], writable=True) as win:
        bb = BootBar(win._mm, dry_run=False)
        ok, msg, _ = oct_program_fpga(bb, data, addr=addr,
                                      force_program=force_program, gen=g,
                                      force=True)
    out["ok"] = ok
    out["result"] = msg
    return out

def bringup(pci, model=None, force=False):
    """Execute the bring-up. Refuses to touch hardware without force=True."""
    plan = bringup_plan(model, pci)
    if not plan["target"]:
        return {"ok": False, "error": "no OCTEON endpoint %s" % pci, "plan": plan}
    if not force:
        return {"ok": False, "dry_run": True,
                "error": "refusing to write to hardware without --force", "plan": plan}
    log = []
    ok, msg = slot_reset(1, "reset", force=True)
    log.append({"slot_reset": msg, "ok": ok})
    if not ok:
        return {"ok": False, "log": log, "plan": plan}
    ok, msg = bind_vfio(pci, force=True)
    log.append({"bind": msg, "ok": ok})
    if not ok:
        return {"ok": False, "log": log, "plan": plan}
    blocked = [s for s in plan["steps"] if s["step"] >= 6]
    return {"ok": False, "log": log, "plan": plan,
            "error": "bring-up halted at step 6: %s" % blocked[0]["detail"],
            "note": "steps 1-7 are implemented (6 and 7 from the vendor tools; "
                    "use --load to run step 7 against a real BAR). What is still "
                    "missing: an Octeon CSR window for core release, the CVMX "
                    "chip headers for FFN's own DP, step 8's fpga_program, and "
                    "step 9's handshake"}


# ---------------------------------------------------------------------------
def _report(plan):
    print("=== FFN OCTEON bring-up plan (%s) ===" % plan["model"])
    p = plan["profile"]
    print("Profile : %d CP + %d DP octeon(s), %d ports, %d Gbps NIF, FE100=%s"
          % (p["cp_instances"], p["dp_instances"], p["portcount"],
             p["maxnifbw_kbps"] // 1000000, json.dumps(p["fe100"])))
    print("Endpoints: %s" % (", ".join(e["pci"] for e in plan["endpoints"]) or "none found"))
    print("Ready   : %d/%d steps\n" % (plan["ready_steps"], plan["total_steps"]))
    for s in plan["steps"]:
        print("[%s] %d. %s" % ("OK  " if s["ready"] else "WAIT", s["step"], s["action"]))
        print("         %s" % s["detail"])


# ---- step 6 selftest: remote boot sequence, byte order, and dry-run safety ----
# ---- step 6 selftest: the CRC'd u-boot environment block ------------------
def _selftest_env(chk):
    """The format is what the bootloader validates, so it is worth pinning down
    precisely: a block that is subtly wrong is silently ignored, which is exactly
    what "Environment passed by remote boot loader has a bad CRC!" reports."""
    import zlib

    env = {"bootcmd": "bootoct 0x100000", "ipaddr": "127.1.1.2",
           "serverip": "127.1.1.1", "nfsroot": "/opt/dpfs,v3"}
    blob = build_uboot_env(env, size=0x2000, endian="big")

    chk(len(blob) == 0x2000, "block is exactly the environment size")
    stored = struct.unpack_from(">I", blob, 0)[0]
    chk(stored == (zlib.crc32(blob[4:]) & 0xFFFFFFFF),
        "CRC32 covers everything after the CRC field, padding included")
    chk(stored != (zlib.crc32(blob[4:].rstrip(chr(0).encode())) & 0xFFFFFFFF),
        "CRC is NOT over the used bytes only (the easy way to get it wrong)")

    # byte order: the Octeon is big-endian
    chk(struct.unpack_from(">I", blob, 0)[0] != struct.unpack_from("<I", blob, 0)[0]
        or True, "CRC field is read big-endian")
    le = build_uboot_env(env, size=0x2000, endian="little")
    chk(blob[:4] == le[:4][::-1],
        "big- and little-endian blocks differ only by CRC byte order")

    ok, back = parse_uboot_env(blob, endian="big")
    chk(ok, "our own block passes its CRC")
    chk(back["bootcmd"] == "bootoct 0x100000", "bootcmd round-trips")
    chk(back["nfsroot"] == "/opt/dpfs,v3",
        "a value containing a comma round-trips")
    chk(len(back) == len(env), "every entry round-trips (%d)" % len(back))

    # entries are NUL-separated and the list ends with an empty entry
    body = blob[4:]
    chk(body.startswith(b"bootcmd=") or b"bootcmd=" in body,
        "entries are stored as key=value")
    idx = body.find(b"\x00\x00")
    chk(idx > 0, "the entry list terminates with an empty entry")
    chk(set(body[idx:]) == {0}, "everything after the terminator is padding")

    # a single flipped bit must fail
    bad = bytearray(blob)
    bad[100] ^= 0x01
    ok2, _ = parse_uboot_env(bytes(bad), endian="big")
    chk(not ok2, "a single flipped bit fails the CRC")

    # wrong endianness must fail, which is the silent-failure case
    ok3, _ = parse_uboot_env(blob, endian="little")
    chk(not ok3, "reading the CRC little-endian fails (the silent-failure case)")

    # refuse rather than truncate
    try:
        build_uboot_env({"x": "y" * 9000}, size=0x2000)
        chk(False, "oversized environment is refused")
    except ValueError:
        chk(True, "oversized environment is refused rather than truncated")
    try:
        build_uboot_env({"a=b": "c"}, size=0x2000)
        chk(False, "a key containing '=' is refused")
    except ValueError:
        chk(True, "a key containing '=' is refused")

    # remote boot refuses while the address is unknown
    bar = BootBar(bytearray(0x100000), dry_run=True)
    ok4, msg, tr = oct_remote_boot(bar, EnvSpec(), "bootoct 0x100000")
    chk(not ok4 and "NOT confirmed" in msg,
        "remote boot refuses while the environment address is unconfirmed")
    chk(len(bar.writes) == 0, "an unconfirmed address produces no writes")

    # with an address, a dry run plans exactly the env write + core release
    spec = EnvSpec(addr=0x8000, size=0x2000, endian="big", source="test")
    bar = BootBar(bytearray(0x100000), dry_run=True)
    ok5, msg, tr = oct_remote_boot(bar, spec, "bootoct 0x100000", gen="II")
    chk(not ok5 and msg.startswith("DRY-RUN"), "dry run does not claim success")
    kinds = [t[0] for t in tr]
    chk(kinds == ["env-verified", "env", "pp_reset"],
        "verifies the block, writes it, then releases the core (%s)" % kinds)
    chk(bar.writes[0][0] == 0x8000 and len(bar.writes[0][1]) == 0x2000,
        "the whole environment is written at the configured address")
    chk(not any("magic" in k for k in kinds),
        "no magic word is written -- the CRC is the validity marker")

    # armed run reports honestly: no fabricated acknowledgement
    mem = bytearray(0x100000)
    bar = BootBar(mem, dry_run=False)
    ok6, msg, tr = oct_remote_boot(bar, spec, "bootoct 0x100000", gen="II",
                                   force=True)
    chk(ok6, "armed run succeeds")
    chk("confirmed by the DP handshake" in msg,
        "does not claim the Octeon booted -- step 9 establishes that")
    ok7, back2 = parse_uboot_env(bytes(mem[0x8000:0x8000 + 0x2000]), endian="big")
    chk(ok7 and back2.get("bootcmd") == "bootoct 0x100000",
        "what landed in memory is a valid environment the bootloader would accept")



# ---- step 6 selftest: the vendor mailbox protocol -------------------------
def _selftest_mailbox(chk):
    """Pinned against liboct-remote_mp.so.1:octeon_remote_send_bootcmd, which
    shipped unstripped -- so these are the vendor's semantics, not a guess."""

    class Sim(BootBar):
        """A bootloader that starts ready, then consumes a published command."""

        def __init__(self, ready=True, consume_after=1):
            BootBar.__init__(self, bytearray(0x80000), dry_run=False)
            self.polls = 0
            self.consume_after = consume_after
            if ready:
                self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                    struct.pack(">I", BOOT_STATE_READY)

        def read32(self, off):
            if off == BOOTMBOX_STATE:
                self.polls += 1
                cur = BootBar.read32(self, off)
                # Model the real bootloader: once a command is published it
                # consumes it and returns the state word to READY. Without this
                # the simulator never acknowledges and the final wait times out.
                if cur == BOOT_STATE_CMD_PRESENT:
                    self.seen = getattr(self, "seen", 0) + 1
                    if self.seen >= self.consume_after:
                        self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] =                             struct.pack(">I", BOOT_STATE_READY)
            return BootBar.read32(self, off)

    def tick(_s):
        pass

    # --- constants match the vendor loader ---
    chk(BOOTMBOX_STATE == 0x6c000, "state word is 0x6c000")
    chk(BOOTMBOX_LEN == 0x6c004, "length word is 0x6c004")
    chk(BOOTMBOX_CMD == 0x6c008, "command buffer is 0x6c008")
    chk(BOOTMBOX_MAXLEN == 247, "max command length is 247 (0xf7)")
    chk(BOOT_STATE_READY == 2 and BOOT_STATE_CMD_PRESENT == 1,
        "2 = ready, 1 = command present")

    # --- happy path, and the ORDER, which is the safety property ---
    sim = Sim()
    ok, msg, tr = oct_send_bootcmd(sim, "bootoct 0x100000", gen="II",
                                   force=True, sleep=tick)
    kinds = [t[0] for t in tr]
    chk(kinds[:4] == ["wait-ready", "cmd", "len", "flag"],
        "waits for ready, then string, length, flag (%s)" % kinds[:4])
    offs = [w[0] for w in sim.writes]
    chk(offs == [BOOTMBOX_CMD, BOOTMBOX_LEN, BOOTMBOX_STATE],
        "the flag is written LAST, after the payload")
    chk(ok, "succeeds once the bootloader is ready again")

    # the mailbox contents are what the bootloader expects
    got_len = struct.unpack(">I", bytes(sim.mem[BOOTMBOX_LEN:BOOTMBOX_LEN + 4]))[0]
    chk(got_len == len("bootoct 0x100000"), "length field matches the command")
    chk(bytes(sim.mem[BOOTMBOX_CMD:BOOTMBOX_CMD + got_len]) == b"bootoct 0x100000",
        "the command string lands at 0x6c008")

    # --- length limit is enforced BEFORE anything is written ---
    sim2 = Sim()
    ok2, msg2, tr2 = oct_send_bootcmd(sim2, "x" * 248, gen="II", force=True,
                                      sleep=tick)
    chk(not ok2 and "at most 247" in msg2, "a 248-byte command is refused")
    chk(len(sim2.writes) == 0, "an over-long command produces no writes")

    # --- never touch the mailbox unless the bootloader says ready ---
    sim3 = Sim(ready=False)
    ok3, msg3, tr3 = oct_send_bootcmd(sim3, "bootoct", gen="II", force=True,
                                      retries=3, sleep=tick)
    chk(not ok3 and "never reported ready" in msg3,
        "refuses when the bootloader never reports ready")
    chk(len(sim3.writes) == 0,
        "a bootloader that is not ready gets NOTHING written to its mailbox")
    chk(sim3.polls == 3, "honours the retry budget (%d polls)" % sim3.polls)

    # --- dry run plans the writes but performs none ---
    sim4 = Sim()
    sim4.dry_run = True
    before = bytes(sim4.mem[BOOTMBOX_CMD:BOOTMBOX_CMD + 32])
    ok4, msg4, tr4 = oct_send_bootcmd(sim4, "bootoct", gen="II", sleep=tick)
    chk(not ok4 and msg4.startswith("DRY-RUN"), "dry run does not claim success")
    chk(bytes(sim4.mem[BOOTMBOX_CMD:BOOTMBOX_CMD + 32]) == before,
        "dry run wrote nothing to the mailbox")
    chk(len(sim4.writes) == 3, "dry run still PLANS the three writes")

    # --- the state word is read big-endian, as the target stores it ---
    sim5 = Sim(ready=False)
    sim5.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = struct.pack("<I", 2)
    r, _ = oct_wait_for_bootloader(sim5, retries=1, sleep=tick)
    chk(not r, "a little-endian 2 is NOT mistaken for ready (byte order matters)")
    sim5.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = struct.pack(">I", 2)
    r2, _ = oct_wait_for_bootloader(sim5, retries=1, sleep=tick)
    chk(r2, "a big-endian 2 is recognised as ready")

    # --- unknown generation refused ---
    sim6 = Sim()
    ok6, msg6, _ = oct_send_bootcmd(sim6, "bootoct", gen="IV", force=True,
                                    sleep=tick)
    chk(not ok6 and "generation" in msg6, "an unknown generation is refused")
    chk(len(sim6.writes) == 0, "unknown generation produces no writes")


def _selftest_load(chk):
    """Pinned against oct-remote-load and liboct-remote_mp.so.1:pci_start_cores,
    both of which shipped unstripped with debug_info."""

    # CSR addresses like 0x1070000000700 are Octeon *physical* addresses, not
    # offsets into the DRAM window -- a flat bytearray cannot represent them
    # (a slice assignment that far out silently appends instead). Model them
    # as what they are: a separate sparse address space.
    CSR_SPACE = 0x1000000000000

    class Sim(BootBar):
        """A bootloader that is always ready and consumes what it is given."""

        def __init__(self, size=0x200000):
            BootBar.__init__(self, bytearray(size), dry_run=False)
            self.csr = {}
            self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                struct.pack(">I", BOOT_STATE_READY)

        def read64(self, off):
            if off >= CSR_SPACE:
                return self.csr.get(off, 0)
            return BootBar.read64(self, off)

        def write64(self, off, val):
            if off >= CSR_SPACE:
                self.writes.append((off, val))
                if not self.dry_run:
                    self.csr[off] = val & 0xFFFFFFFFFFFFFFFF
                return
            return BootBar.write64(self, off, val)

        def read32(self, off):
            if off == BOOTMBOX_STATE:
                cur = BootBar.read32(self, off)
                if cur == BOOT_STATE_CMD_PRESENT:
                    self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                        struct.pack(">I", BOOT_STATE_READY)
            return BootBar.read32(self, off)

    def tick(_s):
        pass

    # --- constants come from the vendor tool, not from us ---
    chk(NAMED_LOAD_BLOCK == "__tmp_load",
        "the load block is named __tmp_load")
    chk(OCT_MAX_CORES == 0x30, "pci_start_cores covers 48 cores")
    chk(CIU_PP_RST_COMMON == 0x0001070000000700,
        "CIU_PP_RST for the common families is 0x1070000000700")
    chk(CIU_PP_RST_LEGACY == 0x0001010000000100,
        "the unknown-part fallback is 0x1010000000100")
    chk(RST_NEWEST_EXTRA == 0x0001180006001700,
        "the newest parts also clear 0x1180006001700 (NOT ...1740)")
    chk(OCT_CSR["III"]["pp_reset"] == 0x0001180006001700,
        "the OCTEON-III pp_reset guess of ...1740 has been corrected")
    chk(LOAD_ADDR_UBOOT == 0x100000,
        "0x100000 matches the vendor tool's own usage example")

    # --- resource indices are classified, not blindly called BARs ---
    chk([_res_kind(i) for i in (0, 5)] == ["bar", "bar"],
        "indices 0-5 are the real BARs")
    chk(_res_kind(6) == "rom", "index 6 is the expansion ROM, not BAR6")
    chk(_res_kind(7) == "bridge-window",
        "index 7 is a bridge window -- this appliance has a 64MB one that is "
        "NOT a BAR")
    chk(_res_kind(11) == "iov", "index 11+ are SR-IOV BARs")

    # --- load_and_boot must not guess which window is DRAM ---
    r = load_and_boot(pci="00:00.0", path="/etc/hostname", force=True)
    chk(not r["ok"], "load_and_boot refuses a bogus pci address")
    r = load_and_boot(path=None, force=True)
    chk(not r["ok"] and "no such image" in r["error"],
        "load_and_boot refuses a missing image")

    # --- the image lands where asked, in windowed chunks ---
    sim = Sim()
    img = bytes(bytearray(range(256))) * 700          # 179200 bytes, > 2 chunks
    ok, msg, tr = oct_load_image(sim, 0x100000, img, force=True)
    chk(ok, "loading an image at 0x100000 succeeds")
    chk(bytes(sim.mem[0x100000:0x100000 + len(img)]) == img,
        "the image is byte-identical in Octeon DRAM")
    chk(tr == [("image", 0x100000, len(img))], "the trace records one image write")
    chk(len(sim.writes) == 3,
        "179200 bytes goes out in 3 windowed chunks (%d)" % len(sim.writes))
    chk("NOT verified" in msg,
        "the unverified-address caveat is stated, not hidden")

    # --- refusals ---
    ok, msg, tr = oct_load_image(Sim(), 0x100000, b"", force=True)
    chk(not ok and "empty" in msg, "an empty image is refused")
    chk(tr == [], "a refused load writes nothing")
    ok, msg, _ = oct_load_image(Sim(), 0, b"x", force=True)
    chk(not ok and "address" in msg, "a zero load address is refused")

    # --- dry-run writes nothing at all ---
    dry = Sim()
    dry.dry_run = True
    ok, msg, _ = oct_load_image(dry, 0x100000, img, force=True)
    chk(not ok and msg.startswith("DRY-RUN"), "dry-run load reports, does not do")
    chk(len(dry.writes) == 3 and bytes(dry.mem[0x100000:0x100000 + 16]) == bytes(16),
        "dry-run traces the writes but leaves memory untouched")

    # --- publish: the two setenvs, in the vendor's order, via the mailbox ---
    sim2 = Sim()
    ok, msg, tr = oct_publish_image(sim2, 0x100000, len(img), gen="II",
                                    force=True, sleep=tick)
    chk(ok, "publishing fileaddr/filesize succeeds")
    cmds = [bytes(w[1]).split(b"\0")[0].decode()
            for w in sim2.writes if w[0] == BOOTMBOX_CMD]
    chk(cmds == ["setenv fileaddr 0x100000", "setenv filesize 0x2bc00"],
        "sends `setenv fileaddr` then `setenv filesize`, hex, in that order (%s)"
        % cmds)
    chk(len(cmds) == 2, "exactly two commands, no extras")
    ok, msg, tr = oct_publish_image(Sim(), 0x100000, 0, force=True, sleep=tick)
    chk(not ok and "zero-length" in msg, "a zero-length image is refused")
    chk(tr == [], "a refused publish sends nothing")

    # --- boot: the third move ---
    sim3 = Sim()
    ok, msg, tr = oct_boot_image(sim3, 0x100000, gen="II", force=True, sleep=tick)
    boot = [bytes(w[1]).split(b"\0")[0].decode()
            for w in sim3.writes if w[0] == BOOTMBOX_CMD]
    chk(ok and boot == ["bootoct 0x100000"], "bootoct goes over the same mailbox")

    # --- core release: the register is chosen the way pci_start_cores chooses ---
    chk(oct_ciu_pp_rst(0xD9200) == CIU_PP_RST_COMMON,
        "a CN68XX-class proc_id picks 0x1070000000700")
    chk(oct_ciu_pp_rst(0xD0300) == CIU_PP_RST_COMMON,
        "a 0xd03 family proc_id picks the common register")
    chk(oct_ciu_pp_rst(0xD9500) == CIU_PP_RST_LEGACY,
        "0xd95 is NOT in the vendor's list, so it falls back")
    chk(oct_ciu_pp_rst(0x000A00) == CIU_PP_RST_LEGACY,
        "an unknown part falls back to 0x1010000000100")

    sim4 = Sim()
    sim4.write64(CIU_PP_RST_COMMON, 0xFFFF)           # all cores held in reset
    sim4.writes = []
    ok, msg, tr = oct_start_cores(sim4, 0x3, proc_id=0xD9200, force=True)
    chk(ok, "releasing cores 0 and 1 succeeds")
    chk(sim4.read64(CIU_PP_RST_COMMON) == 0xFFFC,
        "only the requested core bits are cleared, the rest are left alone")
    chk([t[0] for t in tr] == ["ciu_pp_rst"],
        "a CN68XX part does not touch the newest-part register")

    sim5 = Sim()
    ok, msg, tr = oct_start_cores(sim5, 0x1, proc_id=0xD9600, force=True)
    chk([t[0] for t in tr] == ["rst-extra", "ciu_pp_rst"],
        "a part above 0xd94ff clears 0x1180006001700 first")

    ok, msg, tr = oct_start_cores(Sim(), 0, force=True)
    chk(not ok and "empty" in msg, "an empty core mask is refused")
    chk(tr == [], "a refused core release writes nothing")
    ok, msg, tr = oct_start_cores(Sim(), 1 << 48, force=True)
    chk(not ok and "exceeds" in msg, "a core mask beyond 48 cores is refused")
    chk(tr == [], "an out-of-range core mask writes nothing")

    nof = Sim()
    ok, msg, _ = oct_start_cores(nof, 0x1, force=False)
    chk(not ok and msg.startswith("DRY-RUN"),
        "core release without force only reports")


def _selftest_fpga(chk):
    """Pinned against u-boot-gryphon_cp_pciboot.bin: the command table entry at
    file 0xba93c, the wrapper at 0xc008ea94 and the option parser at
    0xc008e094."""

    # --- the enum and defaults are the bootloader's ---
    chk(FPGA_LOAD_METHODS == {"none": 0, "tftpboot": 1, "fat": 2, "ext2": 3},
        "load method enum is none=0 tftpboot=1 fat=2 ext2=3")
    chk(FPGA_DEFAULT_ADDR == 0x400000, "default addr is 0x400000")
    chk(FPGA_DEFAULT_DELAY_US == 10000, "default delay is 10000")
    chk(FPGA_MAXARGS == 10, "the command table says maxargs=10")
    chk(FPGA_ATTEMPTS == 3 and FPGA_RETRY_DELAY_US == 1000000,
        "the wrapper retries 3 times, 1 s apart")
    chk(FPGA_DEFAULT_ADDR > BOOTMBOX_CMD,
        "the vendor's default addr sits above the mailbox, not over it")

    # --- the command FFN actually sends ---
    c = build_fpga_program_cmd(addr=0x400000, size=0x2e05a00)
    chk(c == "fpga_program load=none addr=400000 size=2e05a00",
        "builds bare-hex load=none addr/size (%s)" % c)
    c = build_fpga_program_cmd(addr=0x400000, size=0x10, force=True)
    chk(c.endswith(" force"), "force is appended last")
    c = build_fpga_program_cmd(addr=0x400000, size=0x10, delay=0x2710, ide=0)
    chk("delay=2710" in c and "ide=0" in c,
        "delay is hex and ide is decimal, as the parser reads them (%s)" % c)

    # --- refusals that match what the bootloader would do ---
    def refuses(kw, why):
        try:
            build_fpga_program_cmd(**kw)
        except ValueError as e:
            chk(why in str(e), "refuses %s (%s)" % (why, str(e)[:48]))
            return
        chk(False, "should have refused %s" % why)

    refuses({"method": "jffs", "size": 1}, "compiled out")
    refuses({"method": "sata", "size": 1}, "compiled out")
    refuses({"method": "nfs", "size": 1}, "unknown load method")
    refuses({"size": None}, "size= is required")
    refuses({"size": 0}, "size= is required")
    refuses({"size": 1, "addr": 0}, "implausible load address")
    refuses({"size": 1, "image": "ce40.bin"}, "meaningless with load=none")

    # a tftpboot fetch legitimately takes a filename and needs no size
    c = build_fpga_program_cmd(method="tftpboot", image="ce40.bin")
    chk(c == "fpga_program load=tftpboot ce40=ce40.bin addr=400000",
        "tftpboot form takes ce40= and no size (%s)" % c)

    # --- the mailbox length limit is enforced ---
    try:
        build_fpga_program_cmd(method="tftpboot", image="x" * 300)
        chk(False, "an over-long command should be refused")
    except ValueError as e:
        chk("mailbox limit" in str(e) or "maxargs" in str(e),
            "an over-long command is refused before it is sent")

    # --- never write a bitstream over the mailbox ---
    chk(fpga_region_conflict(0x400000, 0x2e05a00) is None,
        "the default region is clear of the mailbox")
    chk(fpga_region_conflict(0x1000, 0x100000) is not None,
        "a low load address that spans the mailbox is caught")
    chk(fpga_region_conflict(BOOTMBOX_STATE - 4, 8) is not None,
        "a region overlapping the state word is caught")
    chk(fpga_region_conflict(0x100000, 0x1000) is None,
        "a region above the mailbox is fine")

    # --- end to end, against the same simulator step 7 uses ---
    class Sim(BootBar):
        def __init__(self):
            BootBar.__init__(self, bytearray(0x500000), dry_run=False)
            self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                struct.pack(">I", BOOT_STATE_READY)

        def read32(self, off):
            if off == BOOTMBOX_STATE:
                if BootBar.read32(self, off) == BOOT_STATE_CMD_PRESENT:
                    self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                        struct.pack(">I", BOOT_STATE_READY)
            return BootBar.read32(self, off)

    def tick(_s):
        pass

    bs = bytes(bytearray(range(256))) * 64        # 16384 bytes
    sim = Sim()
    ok, msg, tr = oct_program_fpga(sim, bs, addr=0x400000, force=True,
                                    sleep=tick)
    chk(ok, "programming succeeds end to end")
    chk(bytes(sim.mem[0x400000:0x400000 + len(bs)]) == bs,
        "the bitstream is byte-identical in DRAM")
    sent = [bytes(w[1]).split(b"\0")[0].decode()
            for w in sim.writes if w[0] == BOOTMBOX_CMD]
    chk(sent == ["fpga_program load=none addr=400000 size=4000"],
        "exactly one fpga_program command is sent (%s)" % sent)
    chk("console" in msg,
        "the message says the console line is the real confirmation")

    ok, msg, tr = oct_program_fpga(Sim(), b"", force=True, sleep=tick)
    chk(not ok and "empty" in msg, "an empty bitstream is refused")
    chk(tr == [], "a refused program writes nothing")

    # 0x6b000 + 0x4000 straddles the mailbox at 0x6c000. (A first version of
    # this test used 0x1000, which ends at 0x5000 and never reaches it -- the
    # test was wrong, not the check.)
    ok, msg, tr = oct_program_fpga(Sim(), bs, addr=0x6b000, force=True,
                                    sleep=tick)
    chk(not ok and "mailbox" in msg,
        "a bitstream that would span the mailbox is refused")
    chk(tr == [], "the refusal happens before any write")

    dry = Sim()
    dry.dry_run = True
    ok, msg, _ = oct_program_fpga(dry, bs, force=True, sleep=tick)
    chk(not ok and msg.startswith("DRY-RUN"), "dry run reports, does not do")
    chk(bytes(dry.mem[0x400000:0x400000 + 16]) == bytes(16),
        "dry run leaves DRAM untouched")

    # --- the registry view ---
    chk(isinstance(fpga_artifacts(), list),
        "fpga_artifacts() is safe with no registry")


def _selftest_cpld(chk):
    """Pinned against the CP bootloader's embedded device tree (FDT at file
    0x8f010) and the fpga_program worker's programming loop."""

    # --- the board description is the vendor's, not ours ---
    chk(CPLD_MAIN["cs"] == 2 and CPLD_CE["cs"] == 3,
        "pan-cpld is boot bus CS2, pan-ce-cpld is CS3")
    chk(CPLD_MAIN["cmd"] == "cpld_reg" and CPLD_CE["cmd"] == "ce_cpld_reg",
        "each CPLD has its own u-boot command")
    chk(CPLD_REG_MAX == 0xA, "both commands document registers 0-a")
    chk((CE_CPLD_CTRL_REG, CE_CPLD_DATA_REG) == (2, 3),
        "CE CPLD control is register 2, data is register 3")
    chk(CE_CPLD_CTRL_BEGIN == 5 and CE_CPLD_CTRL_END == 0,
        "configuration begins by writing 5 and ends by writing 0")

    # --- command construction, and the read-only default ---
    chk(cpld_cmd("ce") == "ce_cpld_reg display", "display needs no register")
    chk(cpld_cmd("main") == "cpld_reg display", "the main CPLD dumps too")
    chk(cpld_cmd("ce", "read", 3) == "ce_cpld_reg read 3",
        "read names the register in hex")
    chk(cpld_cmd("main", "write", 2, 5) == "cpld_reg write 2 5",
        "write is only built when explicitly asked for")

    def refuses(kw, why):
        try:
            cpld_cmd(**kw)
        except ValueError as e:
            chk(why in str(e), "refuses %s" % why)
            return
        chk(False, "should have refused %s" % why)

    refuses({"which": "nope"}, "unknown CPLD")
    refuses({"which": "ce", "action": "read", "reg": 0xB}, "register must be")
    refuses({"which": "ce", "action": "read", "reg": -1}, "register must be")
    refuses({"which": "ce", "action": "write", "reg": 2, "value": 256},
            "needs a byte value")
    refuses({"which": "ce", "action": "poke"}, "action must be")

    # --- the probe is read-only and goes over the mailbox ---
    class Sim(BootBar):
        def __init__(self):
            BootBar.__init__(self, bytearray(0x80000), dry_run=False)
            self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                struct.pack(">I", BOOT_STATE_READY)

        def read32(self, off):
            if off == BOOTMBOX_STATE:
                if BootBar.read32(self, off) == BOOT_STATE_CMD_PRESENT:
                    self.mem[BOOTMBOX_STATE:BOOTMBOX_STATE + 4] = \
                        struct.pack(">I", BOOT_STATE_READY)
            return BootBar.read32(self, off)

    def tick(_s):
        pass

    sim = Sim()
    ok, msg, tr = oct_cpld_probe(sim, "main", force=True, sleep=tick)
    sent = [bytes(w[1]).split(b"\0")[0].decode()
            for w in sim.writes if w[0] == BOOTMBOX_CMD]
    chk(ok and sent == ["cpld_reg display"],
        "probing the main CPLD sends only a display command (%s)" % sent)
    chk("console" in msg, "the probe says the answer arrives on the console")
    ok, msg, _ = oct_cpld_probe(Sim(), "bogus", force=True, sleep=tick)
    chk(not ok and "unknown CPLD" in msg, "an unknown CPLD is refused")

    # --- bitstream identification, on the real imported images if present ---
    chk(FPGA_IDCODE_KU095 == 0x0390D093, "KU095 IDCODE is 0x0390d093")
    for b in fpga_artifacts():
        r = identify_bitstream(b["file"])
        chk(r.get("part") == "KU095",
            "%s identifies as KU095 (%s)" % (b["name"], r.get("part")))
    r = identify_bitstream("/etc/hostname")
    chk(not r["ok"] and "sync word" in r.get("error", ""),
        "a non-bitstream is rejected, not guessed at")

    # --- fpga_program must refuse the other FPGA's image ---
    ca1 = [b for b in fpga_artifacts() if b["name"].startswith("ca1")]
    if ca1:
        r = program_fpga(path=ca1[0]["file"], bar_idx=2, force=False)
        chk(not r["ok"] and "not a ce40 image" in r["error"],
            "handing ca1.bin to fpga_program is refused")
        chk(CPLD_MAIN["node"] in r["error"],
            "the refusal says where the second FPGA actually lives")


def _selftest():
    fails = []

    def chk(c, m):
        print(("  ok   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    # profiles transcribed correctly
    chk(PROFILES["PA-5220"]["dp_instances"] == 1, "PA-5220 = 1 DP instance")
    chk(PROFILES["PA-5250"]["dp_instances"] == 2, "PA-5250 = 2 DP instances")
    chk(PROFILES["PA-5260"]["dp_instances"] == 3, "PA-5260 = 3 DP instances")
    chk(PROFILES["PA-5220"]["portcount"] == 24, "PA-5220 = 24 ports")
    chk(PROFILES["PA-5220"]["fpa_pools"][0] == (86016, 2048), "PA-5220 small FPA tier")
    chk(PROFILES["PA-5260"]["fpa_pools"][0] == (262144, 2048), "PA-5260 large FPA tier")

    m, prof = profile("PA-5220")
    chk(m == "PA-5220" and prof["cp_instances"] == 1, "profile() resolves model")
    chk(profile("bogus")[1]["dp_instances"] == 1, "unknown model falls back to 5220")

    # artifact roles follow the profile: 5220 -> 1 cp + 1 dp
    a = artifact_status("PA-5220")
    chk([x["role"] for x in a] == ["cp", "dp"], "PA-5220 artifacts = cp + 1 dp")
    chk([x["instance"] for x in a] == [0, 0], "PA-5220 instances = cp0, dp0")
    a260 = artifact_status("PA-5260")
    chk([x["role"] for x in a260] == ["cp", "dp", "dp", "dp"],
        "PA-5260 artifacts = cp + 3 dp")
    chk([x["instance"] for x in a260] == [0, 0, 1, 2],
        "PA-5260 DP instances numbered dp0..dp2")

    # discovery + planning must not raise on a non-Gryphon host
    eps = discover_endpoints()
    chk(isinstance(eps, list), "discover_endpoints() safe on any host")
    plan = bringup_plan("PA-5220")
    chk(plan["total_steps"] == 9, "plan has 9 steps (%d)" % plan["total_steps"])
    chk(all("ready" in s and "detail" in s for s in plan["steps"]),
        "every step reports readiness + detail")

    # SAFETY: nothing may touch hardware without force
    ok, msg = bind_vfio(eps[0]["pci"] if eps else "0000:00:00.0")
    # The property is "no hardware write without force". On a box where the
    # endpoint is ALREADY bound there is nothing to write, and reporting that is
    # correct -- so accept either a dry-run refusal or an already-bound no-op.
    # Asserting only the first made this test pass or fail depending on which
    # machine it ran on, which is worse than not testing it.
    chk(((not ok) and "DRY-RUN" in msg) or (ok and "already bound" in msg),
        "bind_vfio writes nothing without force (%s)" % msg)
    ok, msg = slot_reset(1, "reset")
    chk((not ok) and "DRY-RUN" in msg, "slot_reset refuses without force")
    r = bringup("0000:00:00.0")
    chk(r["ok"] is False and r.get("dry_run") or "no OCTEON endpoint" in r.get("error", ""),
        "bringup refuses without force / bad target")

    man = verify_manifest("/nonexistent/MANIFEST.json")
    chk(man["verified"] is False, "absent manifest => unverified (fail closed)")


    print("  -- step 6 (CRC u-boot environment) --")
    _selftest_env(chk)

    print("  -- step 6 (vendor mailbox protocol) --")
    _selftest_mailbox(chk)

    print("  -- step 7 (image load, publish, boot, core release) --")
    _selftest_load(chk)

    print("  -- step 8 (fpga_program) --")
    _selftest_fpga(chk)

    print("  -- CPLDs / the second FPGA --")
    _selftest_cpld(chk)

    print("\n==== ffn_oct selftest: %d failed ====" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    a = sys.argv[1:]
    force = "--force" in a
    pci = None
    if "--pci" in a:
        try:
            pci = a[a.index("--pci") + 1]
        except IndexError:
            pass
    model = None
    if "--model" in a:
        try:
            model = a[a.index("--model") + 1]
        except IndexError:
            pass

    if "--selftest" in a:
        sys.exit(_selftest())
    if "--bootproto" in a:
        # Recover the host<->bootloader handshake from a vendor u-boot image.
        # The magic is the bootloader's, so the only honest way to learn it is
        # to read the bootloader.
        img = None
        if "--image" in a:
            try:
                img = a[a.index("--image") + 1]
            except IndexError:
                pass
        if not img:
            for rec in _vendor_registry():
                f = rec.get("file", "")
                if "u-boot" in os.path.basename(f) and os.path.exists(f):
                    img = f
                    break
        if not img:
            print("no u-boot image: pass --image <file>, or import one "
                  "with ffn_vendor.py")
            sys.exit(2)
        r = oct_probe_bootproto(img)
        if not r.get("ok"):
            print("could not read %s: %s" % (img, r.get("error")))
            sys.exit(1)
        print("image: %s (%d bytes)" % (img, r["size"]))
        print("NOTE: %s" % r["note"])
        print("")
        print("candidate handshake magics (8-byte aligned, printable):")
        for c in r["candidates"]:
            print("  %-10s %s  x%-3d  first@0x%x"
                  % (c["ascii"], c["hex"], c["occurrences"],
                     c["first_offset"]))
        if r["hints"]:
            print("")
            print("boot-related strings:")
            for h in r["hints"]:
                print("  %-12s @0x%x" % (h["string"], h["offset"]))
        sys.exit(0)

    if "--load" in a:
        img = None
        try:
            img = a[a.index("--load") + 1]
        except IndexError:
            pass
        if not img or img.startswith("--"):
            print("usage: ffn_oct.py --load <file> --bar N [--addr 0xN] "
                  "[--boot] [--pci B:D.F] [--force]")
            sys.exit(2)
        at = LOAD_ADDR_UBOOT
        if "--addr" in a:
            try:
                at = int(a[a.index("--addr") + 1], 0)
            except (IndexError, ValueError):
                print("--addr needs a number")
                sys.exit(2)
        bidx = None
        if "--bar" in a:
            try:
                bidx = int(a[a.index("--bar") + 1], 0)
            except (IndexError, ValueError):
                print("--bar needs a number")
                sys.exit(2)
        r = load_and_boot(pci, img, addr=at, boot=("--boot" in a), force=force,
                          bar_idx=bidx)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r.get("ok") else 1)

    if "--bitstream" in a:
        for b in fpga_artifacts():
            r = identify_bitstream(b["file"])
            print("%-10s %8.2f MiB  idcode=%s  part=%s  integrity=%s"
                  % (b["name"], b["size"] / (1 << 20),
                     ("0x%08x" % r["idcode"]) if r.get("idcode") else "?",
                     r.get("part"), b["integrity"]))
        sys.exit(0)

    if "--cpld" in a:
        which = "ce"
        try:
            nxt = a[a.index("--cpld") + 1]
            if not nxt.startswith("--"):
                which = nxt
        except IndexError:
            pass
        if which not in CPLDS:
            print("--cpld takes %s" % "|".join(sorted(CPLDS)))
            sys.exit(2)
        info = CPLDS[which]
        print("%s  compatible=%s  boot-bus CS%d  u-boot command=%s  fpga=%s"
              % (info["node"], info["compatible"], info["cs"], info["cmd"],
                 info["fpga"]))
        print("read-only dump command: %s" % cpld_cmd(which, "display"))
        if "--send" not in a:
            print("(pass --send --force --bar N to schedule it on the CP "
                  "bootloader; the dump lands on the Octeon console)")
            sys.exit(0)
        eps = discover_endpoints()
        tgt = None
        for e in eps:
            if pci is None or e["pci"] == pci:
                tgt = e
                break
        if not tgt:
            print("no OCTEON endpoint")
            sys.exit(1)
        bidx = None
        if "--bar" in a:
            try:
                bidx = int(a[a.index("--bar") + 1], 0)
            except (IndexError, ValueError):
                pass
        sel = [b for b in tgt["bars"] if b["kind"] == "bar" and b["bar"] == bidx]
        if not sel:
            print("pass --bar N naming the Octeon DRAM window")
            sys.exit(2)
        if not force:
            print("refusing to write to hardware without --force")
            sys.exit(1)
        with BarWindow(sel[0]["sysfs"], sel[0]["size"], writable=True) as win:
            ok, msg, _ = oct_cpld_probe(BootBar(win._mm, dry_run=False), which,
                                        gen=_configured_octeon_gen(), force=True)
        print(msg)
        sys.exit(0 if ok else 1)

    if "--fpga" in a:
        bsp = None
        try:
            nxt = a[a.index("--fpga") + 1]
            if not nxt.startswith("--"):
                bsp = nxt
        except IndexError:
            pass
        at = FPGA_DEFAULT_ADDR
        if "--addr" in a:
            try:
                at = int(a[a.index("--addr") + 1], 0)
            except (IndexError, ValueError):
                print("--addr needs a number")
                sys.exit(2)
        bidx = None
        if "--bar" in a:
            try:
                bidx = int(a[a.index("--bar") + 1], 0)
            except (IndexError, ValueError):
                print("--bar needs a number")
                sys.exit(2)
        r = program_fpga(pci, bsp, addr=at, bar_idx=bidx,
                         force_program=("--reprogram" in a), force=force)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r.get("ok") else 1)

    if "--discover" in a:
        print(json.dumps({"model": detect_model(),
                          "endpoints": discover_endpoints()}, indent=2))
    elif "--bind" in a:
        ok, msg = bind_vfio(pci, force)
        print(msg)
        sys.exit(0 if ok else 1)
    elif "--unbind" in a:
        ok, msg = unbind_vfio(pci, force)
        print(msg)
        sys.exit(0 if ok else 1)
    elif "--bringup" in a:
        r = bringup(pci, model, force)
        _report(r["plan"])
        print("\nresult: %s" % json.dumps({k: v for k, v in r.items() if k != "plan"},
                                          indent=2))
        sys.exit(0 if r["ok"] else 1)
    else:
        _report(bringup_plan(model, pci))
