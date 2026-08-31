#!/usr/bin/env python3
"""ffn-octboot -- boot FFN's own kernel on the OCTEON, through the console broker.

Everything here goes through ffn_octconsoled: commands into
/run/ffn-octeon-console.in, output read back out of
/var/log/ffn-octeon-console.log. Nothing in this tool opens /dev/ttyS1.

That matters. The UART is a single-reader device: when this tool opened the port
directly while the broker also held it, the two readers each took a share of
every reply, so u-boot's output came back as high-entropy garbage and looked
like a dead line or a wrong baud rate. It was neither. One owner, and everyone
else goes through the log -- which also means an operator can watch the same
boot live with:

    tail -f /var/log/ffn-octeon-console.log

usage: ffn_octboot.py [--watch SECONDS] [--kernel PATH] [--addr 0xN]
                      [--fdt 0xN] [--cores N] [--no-stage]
"""
import argparse
import hashlib
import os
import sys
import time

sys.path.insert(0, "/opt/ffn-ngfw-v2")
sys.path.insert(0, "/opt/ffn-ngfw-v2/tools")
import ffn_octdram as od

PCI = "0000:01:00.0"
FIFO = "/run/ffn-octeon-console.in"
CLOG = "/var/log/ffn-octeon-console.log"
KERNEL = "/var/lib/ffn-ngfw/octeon/ffn-vmlinux-octeon3"
# DEV ONLY: vendor userland from this appliance's own /opt/dpfs. Staged in
# DRAM, never embedded in the kernel, never packaged into an FFN image.
OVERLAY = "/var/lib/ffn-ngfw/octeon/dev/ffn-dev-overlay.cpio"


def fifo(cmd):
    with open(FIFO, "w") as f:
        f.write(cmd + "\n")


def logsize():
    return os.path.getsize(CLOG) if os.path.exists(CLOG) else 0


def logread(start):
    with open(CLOG, "rb") as f:
        f.seek(start)
        return f.read()


def clean(blob):
    txt = blob.decode("ascii", "replace").replace("\x00", "")
    return [l.rstrip() for l in txt.replace("\r", "\n").split("\n")]


def prompt_ok(quiet=False):
    """Ask u-boot to identify itself and see whether anything sane replies."""
    start = logsize()
    fifo("version")
    time.sleep(3.0)
    blob = logread(start)
    if not blob:
        return False, "no reply at all -- the Octeon is not at a u-boot prompt"
    good = sum(1 for c in blob if 32 <= c < 127 or c in (9, 10, 13))
    pct = 100.0 * good / len(blob)
    txt = "\n".join(clean(blob))
    if pct < 90:
        return False, "reply was %.0f%% printable -- another reader has the port" % pct
    if "#" not in txt and "U-Boot" not in txt:
        return False, "reply had no prompt: %r" % txt[-60:]
    return True, txt.strip().split("\n")[-1][:70]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, default=115.0)
    ap.add_argument("--kernel", default=KERNEL)
    ap.add_argument("--addr", default="0x21000000")
    ap.add_argument("--fdt", default="0x80000")
    ap.add_argument("--cores", type=int, default=8)
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("--overlay", default=OVERLAY)
    ap.add_argument("--overlay-addr", default="0x22000000")
    # Extra kernel command-line words, appended verbatim.
    #
    # The reason this exists: booting with no mem= at all leaves the kernel
    # with only ~432 MB of the 8 GB this CP has, because it takes whatever the
    # boot descriptor offers. A suffix-less "mem=2048" is what must be avoided
    # -- arch/mips/cavium-octeon/setup.c parses it with memparse(), so a bare
    # number is BYTES. "mem=2G" or "mem=2048M" is correct and gives the kernel
    # the memory a full rootfs needs to unpack into.
    ap.add_argument("--extra", default="",
                    help="extra kernel args, e.g. --extra 'mem=2G'")
    ap.add_argument("--no-overlay", action="store_true")
    a = ap.parse_args()
    addr = int(a.addr, 0)

    if not os.path.exists(FIFO):
        print("console broker is not running -- start it with:")
        print("  python3 tools/ffn_octconsoled.py start")
        return 2

    print("=== u-boot prompt (via the broker) ===")
    ok, why = prompt_ok()
    print("  %s" % why)
    if not ok:
        print("  reset the Octeon first: "
              "python3 tools/ffn_octctl.py boot --dev 0 --force")
        return 2

    if not a.no_stage:
        print()
        print("=== stage FFN's kernel ===")
        data = open(a.kernel, "rb").read()
        want = hashlib.sha256(data).hexdigest()
        with od.WindowedDram(PCI) as w:
            w.write(addr, data)
            got = hashlib.sha256(w.read(addr, len(data))).hexdigest()
        print("  %.2f MiB -> 0x%x  sha256 %s"
              % (len(data) / (1 << 20), addr, "MATCH" if got == want else "MISMATCH"))
        if got != want:
            return 1

    rootfs_arg = ""
    if not a.no_overlay and os.path.exists(a.overlay):
        oaddr = int(a.overlay_addr, 0)
        print()
        print("=== stage dev overlay rootfs (DEV ONLY -- never packaged) ===")
        blob = open(a.overlay, "rb").read()
        if blob[:6] != b"070701":
            print("  not a newc cpio (magic %r) -- refusing" % blob[:6])
            return 1
        want = hashlib.sha256(blob).hexdigest()
        with od.WindowedDram(PCI) as w:
            w.write(oaddr, blob)
            got = hashlib.sha256(w.read(oaddr, len(blob))).hexdigest()
        ok = got == want
        print("  %.2f MiB -> 0x%x  sha256 %s"
              % (len(blob) / (1 << 20), oaddr, "MATCH" if ok else "MISMATCH"))
        if not ok:
            return 1
        rootfs_arg = " ffn_rootfs=0x%x,0x%x" % (oaddr, len(blob))
    elif not a.no_overlay:
        print()
        print("=== no dev overlay at %s -- booting without a shell ===" % a.overlay)

    # ffn_fdt: the SDK ships built-in trees only for CN3xxx/CN68xx, both legacy
    # CIU. This is a CIU3 part, so it must use the tree u-boot built for the
    # board. keep_bootcon: a XR17V35X on the Octeon's own PCIe bus claims the
    # name ttyS0, so the console handover moves output to that chip -- keeping
    # the boot console means the internal UART keeps reporting.
    boot = ("bootoctlinux 0x%x numcores=%d console=ttyS0,115200n8 "
            "ffn_fdt=%s%s rw%s" % (addr, a.cores, a.fdt, rootfs_arg,
                                   (" " + a.extra) if a.extra else ""))
    print()
    print("=== boot ===")
    print("  %s" % boot)
    print("  (watch live: tail -f %s)" % CLOG)
    start = logsize()
    fifo(boot)

    seen = 0
    deadline = time.time() + a.watch
    while time.time() < deadline:
        time.sleep(2.0)
        blob = logread(start)
        if len(blob) > seen:
            for l in clean(blob[seen:]):
                if l.strip():
                    print("  " + l)
            seen = len(blob)
    print()
    print("=== captured %d bytes ===" % seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
