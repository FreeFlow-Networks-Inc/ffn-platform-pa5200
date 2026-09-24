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
import re
import sys
import time

sys.path.insert(0, "/opt/ffn-ngfw-v2")
sys.path.insert(0, "/opt/ffn-ngfw-v2/tools")
import ffn_octdram as od

PCI = "0000:01:00.0"
FIFO = "/run/ffn-octeon-console.in"
CLOG = "/var/log/ffn-octeon-console.log"


def kernel_bytes(path):
    """Validate a current embedded-initramfs kernel before touching hardware."""
    if os.stat(path).st_size > 512 * 1024**2:
        raise ValueError('Oversized CP kernel')
    with open(path, 'rb') as stream:
        data = stream.read()
    if data[:6] != b'\x7fELF\x02\x02' or data[18:20] != b'\x00\x08':
        raise ValueError('CP kernel must be MIPS64 big-endian ELF')
    versions = {tuple(map(int, m)) for m in re.findall(rb'Linux version (\d+)\.(\d+)\.(\d+)', data)}
    if len(versions) != 1 or next(iter(versions)) < (6, 18, 0):
        raise ValueError('Legacy or unidentified CP kernel rejected')
    return data


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
    ap.add_argument("--kernel", required=True, help="explicit qualified kernel with embedded Debian initramfs")
    ap.add_argument("--addr", default="0x21000000")
    ap.add_argument("--fdt", default="0x80000")
    ap.add_argument("--cores", type=int, default=8)
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("--extra", default="", help="MP-provisioned kernel command line")
    # Accepted for existing callers; overlays are always disabled now.
    ap.add_argument("--no-overlay", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    addr = int(a.addr, 0)
    try:
        data = kernel_bytes(a.kernel)
    except (OSError, ValueError) as exc:
        print(str(exc))
        return 2

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

    print()
    print("=== verify staged kernel ===" if a.no_stage else "=== stage FFN kernel ===")
    want = hashlib.sha256(data).hexdigest()
    with od.WindowedDram(PCI) as w:
        if not a.no_stage:
            w.write(addr, data)
        got = hashlib.sha256(w.read(addr, len(data))).hexdigest()
    print("  %.2f MiB -> 0x%x  sha256 %s"
          % (len(data) / (1 << 20), addr, "MATCH" if got == want else "MISMATCH"))
    if got != want:
        return 1

    # ffn_fdt: the SDK ships built-in trees only for CN3xxx/CN68xx, both legacy
    # CIU. This is a CIU3 part, so it must use the tree u-boot built for the
    # board. keep_bootcon: a XR17V35X on the Octeon's own PCIe bus claims the
    # name ttyS0, so the console handover moves output to that chip -- keeping
    # the boot console means the internal UART keeps reporting.
    # ffn_fdt= is now OPTIONAL. The patched kernel finds the tree by looking up
    # the cvmx_bootmem named block "__fdt" through the descriptor, so passing an
    # address is only needed to override that. Pass --fdt "" to exercise the
    # lookup. Against a kernel WITHOUT that patch, omitting it falls through to
    # the uninitialised octeon_bootinfo->fdt_addr and the boot dies in
    # octeon_irq_init_ciu -- so the two must be deployed together.
    fdt_arg = (" ffn_fdt=%s" % a.fdt) if a.fdt else ""
    boot = ("bootoctlinux 0x%x numcores=%d console=ttyS0,115200n8"
            "%s rw%s" % (addr, a.cores, fdt_arg,
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
