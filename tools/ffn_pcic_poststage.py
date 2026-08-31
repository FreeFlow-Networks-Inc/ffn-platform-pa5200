#!/usr/bin/env python3
"""Watch the OCTEON-written registers after staging, while keeping the ring armed.

Two hazards this has to handle at once, and they pull against each other:

  * u-boot runs octeon_pcie_setup_port during the reset, and the OCTEON tearing
    down on panic clears the SLI too, so the ring loses its configuration
    repeatedly. Without re-arming there is nothing for the far side to consume and
    the window measures nothing.

  * Re-arming resets the ring, which zeroes +0x24. So a naive "did it move since
    the start" test would count the host's own re-arm as consumption -- the same
    class of error as addendum 15.

So: re-arm freely before the staging marker (no measurement), and after it keep
re-arming when the configuration is lost but treat every re-arm as the start of a
new measurement SEGMENT. Movement only counts inside a segment, i.e. between two
re-arms, where nothing the host did could explain it.

  +0x24  consumer index    -- host never writes it
  +0xb0  packets delivered -- host never writes it
  +0x30  input size        -- 512 when armed, 128 when the ring has been reset
"""
import fcntl
import mmap
import os
import re
import struct
import subprocess
import sys
import time

RES = "/sys/bus/pci/devices/0000:01:00.0/resource0"
DEV = "/dev/ffn_pcic"
FFN_PCIC_PROGRAM = (ord("F") << 8) | 3
B = 0x10000
WANT_SIZE = 512
STAT = "/sys/class/net/pcicp0/statistics/%s"

LOG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/vendboot.log"
WATCH_S = int(sys.argv[2]) if len(sys.argv) > 2 else 100

fd = os.open(RES, os.O_RDONLY | os.O_SYNC)
m = mmap.mmap(fd, B + 0x200, prot=mmap.PROT_READ)


def r(o):
    return struct.unpack("<I", m[B + o:B + o + 4])[0]


def regs():
    return {"size": r(0x30), "dbell": r(0x20), "hw_idx": r(0x24),
            "instr": r(0x40), "pkt_cnt": r(0xb0)}


def nic():
    try:
        return (int(open(STAT % "tx_packets").read()),
                int(open(STAT % "rx_packets").read()))
    except OSError:
        return (-1, -1)


def rearm():
    try:
        f = os.open(DEV, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.ioctl(f, FFN_PCIC_PROGRAM)
        return True
    except OSError:
        return False          # -EIO still performs the writes
    finally:
        os.close(f)


def console_has(pat):
    try:
        with open(LOG, "rb") as fh:
            return re.search(pat.encode(), fh.read()) is not None
    except OSError:
        return False


# ---- phase 1: keep it armed, measure nothing ------------------------------
print("  phase 1: holding the ring armed until staging completes")
t0 = time.time()
armed_count = 0
while not console_has(r"=== boot ==="):
    if time.time() - t0 > 700:
        print("  TIMED OUT waiting for staging")
        sys.exit(1)
    if regs()["size"] != WANT_SIZE:
        rearm()
        armed_count += 1
    time.sleep(0.1)

staged = time.time()
if regs()["size"] != WANT_SIZE:
    rearm()
    armed_count += 1
    time.sleep(0.05)
print("  staging done after %.0fs (%d re-arms during it)" % (staged - t0, armed_count))

# ---- phase 2: measure inside re-arm-free segments -------------------------
seg = regs()
seg_start = 0.0
best = {"hw_idx": 0, "pkt_cnt": 0, "instr": 0}
best_at = {"hw_idx": None, "pkt_cnt": None, "instr": None}
segments = 0
notes_seen = set()

print("  BASELINE  size=%d dbell=%d hw_idx=%d instr=%d pkt_cnt=%d"
      % (seg["size"], seg["dbell"], seg["hw_idx"], seg["instr"], seg["pkt_cnt"]))
print()
print("  %7s %5s %7s %7s %7s %8s %5s %5s  %s"
      % ("t", "size", "dbell", "hw_idx", "instr", "pkt_cnt", "tx", "rx", "note"))

last = None
next_ping = 0.0
while time.time() - staged < WATCH_S:
    now = time.time() - staged
    if now >= next_ping:
        subprocess.Popen(["ping", "-c", "2", "-i", "0.2", "-W", "1",
                          "127.1.1.2"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        next_ping = now + 2.0

    cur = regs()
    tx, rx = nic()
    note = ""

    for pat, label in ((r"pcic: module loaded", "OCTEON pcic loaded"),
                       (r"IP-Config: Complete", "IP-Config complete"),
                       (r"Kernel panic", "PANIC")):
        if label not in notes_seen and console_has(pat):
            notes_seen.add(label)
            note = "<- " + label

    # movement within the current segment, before any re-arm perturbs it
    for k in best:
        d = cur[k] - seg[k]
        if d > best[k]:
            best[k] = d
            best_at[k] = now

    if cur["size"] != WANT_SIZE:
        ok = rearm()
        segments += 1
        time.sleep(0.05)
        seg = regs()
        seg_start = now
        note = ("<- ring lost config, RE-ARMED (%s); new segment"
                % ("ok" if ok else "EIO"))
        cur = seg
        tx, rx = nic()

    key = (cur["size"], cur["dbell"], cur["hw_idx"], cur["instr"],
           cur["pkt_cnt"], tx, rx)
    if key != last or note:
        print("  %7.1f %5d %7d %7d %7d %8d %5d %5d  %s"
              % (now, cur["size"], cur["dbell"], cur["hw_idx"], cur["instr"],
                 cur["pkt_cnt"], tx, rx, note))
        sys.stdout.flush()
        last = key
    time.sleep(0.1)

# ---- verdict --------------------------------------------------------------
print()
print("  measured inside re-arm-free segments only (%d re-arms after staging):"
      % segments)
moved = False
for k in ("hw_idx", "pkt_cnt", "instr"):
    at = "" if best_at[k] is None else " (first at t+%.1fs)" % best_at[k]
    print("    %-8s max in-segment rise: %+d%s%s"
          % (k, best[k], at, "   <-- MOVED" if best[k] else ""))
    if best[k]:
        moved = True
etx, erx = nic()
print("    tx during window: %d   rx: %d" % (etx, erx))
print()
if moved:
    print("  VERDICT: a register only the OCTEON writes rose WITHIN a segment"
          " -- consumption is real")
else:
    print("  VERDICT: nothing the OCTEON writes moved; no consumption")
m.close()
os.close(fd)
