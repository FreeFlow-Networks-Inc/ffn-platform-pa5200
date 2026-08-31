#!/usr/bin/env python
"""ffn-dpsh (v2) -- persistent shell SESSION on the DP Octeon, over PCIe.

v1 sent one command at a time and got one reply, so nothing persisted. This talks to
ffn_dpagent2, which keeps a single shell alive on a pty inside the DP and exchanges a
raw byte stream. So cwd sticks between commands, vi and top work, and ^C reaches the
shell. Runs ON the CP (vendor python 2.7); the DP end is /sbin/ffn_dpagent2.

    ffn-dpsh                      interactive session   (ctrl-] to detach)
    ffn-dpsh -c "cd /sbin; pwd"   one command, session state preserved
    ffn-dpsh --status             agent/shell state

LAYOUT (DP phys 0x00400000, 64 KB; big-endian, DP-native):
    0x0000 magic "FFNDPSH2"              0x0020 u32 out_head, pad   (DP)
    0x0008 u32 version, u32 gen          0x0028 u32 out_tail, pad   (CP)
    0x0010 u32 in_head,  pad   (CP)      0x0030 u32 agent_up, shell_alive
    0x0018 u32 in_tail,  pad   (DP)      0x0038 u32 in_size, out_size
    0x1000 in ring 0x2000                0x4000 out ring 0xC000

Head/tail are monotonic counters, not offsets: index = counter % size. Each 8-byte
group is written by exactly one side, because our writes here are read-modify-write
over a whole 64-bit group (the window byte-reverses each one) and would otherwise
clobber the DP's concurrent updates.
"""
from __future__ import print_function

import argparse
import errno
import mmap
import os
import select
import struct
import subprocess
import sys
import termios
import time
import tty

PCI = "0003:03:00.0"
SYSFS = "/sys/bus/pci/devices"
CSR = "/usr/local/cp/oct-remote-csr"
RING_SIZE = 0x10000

OFF_MAGIC, OFF_VERSION, OFF_GEN = 0x0000, 0x0008, 0x000C
OFF_IN_HEAD, OFF_IN_TAIL = 0x0010, 0x0018
OFF_OUT_HEAD, OFF_OUT_TAIL = 0x0020, 0x0028
OFF_AGENT_UP, OFF_SH_ALIVE = 0x0030, 0x0034
OFF_IN_SIZE, OFF_OUT_SIZE = 0x0038, 0x003C
IN_OFF, IN_SIZE = 0x1000, 0x2000
OUT_OFF, OUT_SIZE = 0x4000, 0x8000   # power of two: index is counter % SIZE and the counter wraps at 2**32
MAGIC2 = b"FFNDPSH2"
DETACH = b"\x1d"          # ctrl-]


def swap8(data):
    """Reverse each aligned 8-byte group -- the window's 64-bit byte swap."""
    src = bytearray(data)
    out = bytearray(len(src))
    n = (len(src) // 8) * 8
    for i in range(0, n, 8):
        out[i:i + 8] = src[i:i + 8][::-1]
    if n < len(src):
        out[n:] = src[n:]
    return bytes(out)


class Session(object):
    def __init__(self, program_window=True):
        if program_window:
            self._enable()
            self._window()
        self.fd = os.open(os.path.join(SYSFS, PCI, "resource2"), os.O_RDWR)
        self.mm = mmap.mmap(self.fd, 0x400000 + RING_SIZE, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)
        self.base = 0x400000      # window index 1 -> BAR offset == DP phys

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)

    @staticmethod
    def _enable():
        p = os.path.join(SYSFS, PCI, "enable")
        for v in ("0", "1"):
            try:
                f = open(p, "w")
                try:
                    f.write(v)
                finally:
                    f.close()
            except Exception:
                pass
            time.sleep(0.3)

    @staticmethod
    def _window():
        env = dict(os.environ)
        env["OCTEON_PCI_IDS"] = "0x177d0095"
        env["LD_LIBRARY_PATH"] = "/usr/local/lib64"
        null = open(os.devnull, "w")
        try:
            subprocess.call([CSR, "--devnum=1", "PEM0_BAR1_INDEX1", "0x11"],
                            env=env, stdout=null, stderr=subprocess.STDOUT)
        except Exception:
            pass
        finally:
            null.close()

    def _read(self, off, n):
        a = self.base + off
        start = a & ~7
        lead = a - start
        span = ((lead + n + 7) // 8) * 8
        return swap8(bytes(bytearray(self.mm[start:start + span])))[lead:lead + n]

    def _write(self, off, data):
        a = self.base + off
        start = a & ~7
        lead = a - start
        span = ((lead + len(data) + 7) // 8) * 8
        cur = bytearray(swap8(bytes(bytearray(self.mm[start:start + span]))))
        cur[lead:lead + len(data)] = bytearray(data)
        self.mm[start:start + span] = swap8(bytes(cur))

    def u32(self, off):
        return struct.unpack(">I", self._read(off, 4))[0]

    def set_u32(self, off, v):
        self._write(off, struct.pack(">I", v & 0xFFFFFFFF))

    # -- protocol ------------------------------------------------------------
    def check(self):
        m = self._read(OFF_MAGIC, 8)
        if m == b"\xff" * 8:
            return "window not answering (all-ones)"
        if m == b"FFNDPSH1":
            return ("this DP is running the v1 request/response agent; boot the "
                    "kernel carrying /sbin/ffn_dpagent2 for a session")
        if m != MAGIC2:
            return "no agent: magic is %r" % m
        # The DP publishes its ring geometry; verify it matches ours. A one-sided
        # size change would otherwise corrupt silently -- both sides derive the
        # index as counter % SIZE, so disagreeing sizes read and write different
        # bytes with no error raised anywhere.
        dp_in = self.u32(OFF_IN_SIZE)
        dp_out = self.u32(OFF_OUT_SIZE)
        if (dp_in, dp_out) != (IN_SIZE, OUT_SIZE):
            return ("ring size mismatch: the DP has in=0x%x out=0x%x but this "
                    "client has in=0x%x out=0x%x -- rebuild both sides together"
                    % (dp_in, dp_out, IN_SIZE, OUT_SIZE))
        return None

    def status(self):
        return dict(version=self.u32(OFF_VERSION), gen=self.u32(OFF_GEN),
                    agent_up=self.u32(OFF_AGENT_UP),
                    shell_alive=self.u32(OFF_SH_ALIVE),
                    in_head=self.u32(OFF_IN_HEAD), in_tail=self.u32(OFF_IN_TAIL),
                    out_head=self.u32(OFF_OUT_HEAD),
                    out_tail=self.u32(OFF_OUT_TAIL))

    def send(self, data):
        """Push bytes toward the shell; blocks briefly if the DP is behind."""
        for _ in range(200):
            head = self.u32(OFF_IN_HEAD)
            tail = self.u32(OFF_IN_TAIL)
            space = IN_SIZE - ((head - tail) & 0xFFFFFFFF)
            if space >= len(data):
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("DP is not draining input (shell stuck?)")
        idx = head % IN_SIZE
        first = min(len(data), IN_SIZE - idx)
        self._write(IN_OFF + idx, data[:first])
        if first < len(data):
            self._write(IN_OFF, data[first:])          # wrapped
        self.set_u32(OFF_IN_HEAD, head + len(data))    # publish LAST

    def recv(self, limit=8192):
        """Drain whatever the shell has produced. b'' when idle."""
        head = self.u32(OFF_OUT_HEAD)
        tail = self.u32(OFF_OUT_TAIL)
        avail = (head - tail) & 0xFFFFFFFF
        if not avail:
            return b""
        if avail > OUT_SIZE:
            # Should be unreachable now that the DP publishes counters atomically.
            # Say so rather than silently replaying the ring, which reaches the
            # user as duplicated output with no explanation.
            sys.stderr.write("ffn-dpsh: out-ring resync "
                             "(head=%u tail=%u avail=%u) -- report this\n"
                             % (head, tail, avail))
            tail = head - OUT_SIZE
            avail = OUT_SIZE
        n = min(avail, limit)
        idx = tail % OUT_SIZE
        first = min(n, OUT_SIZE - idx)
        out = self._read(OUT_OFF + idx, first)
        if first < n:
            out += self._read(OUT_OFF, n - first)
        self.set_u32(OFF_OUT_TAIL, tail + n)
        return out


def interactive(s):
    print("ffn-dpsh: session on the DP. ctrl-] detaches (the shell keeps running).")
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    saved = None
    try:
        saved = termios.tcgetattr(fd)
        tty.setraw(fd)
    except Exception:
        saved = None                    # not a tty (piped input) -- fine
    s.send(b"\n")                       # nudge a prompt out
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.02)
            if r:
                data = os.read(fd, 1024)
                if not data:
                    break
                if DETACH in data:
                    data = data[:data.index(DETACH)]
                    if data:
                        s.send(data)
                    break
                s.send(data)
            out = s.recv()
            if out:
                os.write(sys.stdout.fileno(), out)
    except (IOError, OSError) as exc:
        if getattr(exc, "errno", None) != errno.EINTR:
            raise
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print("\r\nffn-dpsh: detached.")


def one_shot(s, cmd, timeout):
    """Run one command in the LIVE session, so state persists across calls.

    The marker MUST NOT appear in the line we send. The pty echoes that line straight
    back, so a marker embedded in it shows up in the stream immediately -- long before
    the command has run -- and waiting for the bare marker then ends the wait on the
    echo. That was the whole intermittent-failure bug: proven by dumping the raw
    stream, where the marker appeared TWICE (once in the echo, once in the output).
    It looked like a 20% flake only because a fast command's echo and output usually
    arrive inside the same poll.

    sh concatenates adjacent quoted strings, so sending "@@F""FN<tag>_$?@@" puts
    @@FFN<tag>_0@@ on stdout while the echoed line only ever shows the split form.
    The marker can then only ever match real output -- the race is gone by
    construction, not made less likely.

    The tag carries a timestamp as well as the pid so a marker left over from an
    earlier call can never satisfy this one.
    """
    tag = "%04x%04x" % (os.getpid() & 0xFFFF, int(time.time()) & 0xFFFF)
    needle = "@@FFN" + tag + "_"
    sent = cmd + '; echo "@@F""FN' + tag + '_$?@@"\n'
    if needle in sent:
        raise RuntimeError("marker leaked into the command line; refusing")

    # Drop anything still in flight from a previous call, bounded so a flooding
    # agent cannot spin us here forever.
    for _ in range(64):
        if not s.recv(OUT_SIZE):
            break

    s.send(sent.encode())

    buf = b""
    deadline = time.time() + timeout
    found = False
    while time.time() < deadline:
        chunk = s.recv(OUT_SIZE)
        if chunk:
            buf += chunk
            if needle.encode() in buf:
                found = True
                break
        else:
            time.sleep(0.01)
    if not found:
        got = buf.decode("ascii", "replace").replace("\r\n", "\n").strip()
        raise RuntimeError("no marker within %.0fs; partial output was:\n%s"
                           % (timeout, got))

    text = buf.decode("ascii", "replace").replace("\r\n", "\n")

    # The first line is the pty's echo of our own command line: drop exactly that,
    # rather than guessing which lines look like the command.
    nl = text.find("\n")
    body = text[nl + 1:] if nl >= 0 else text

    # Cut at the marker and read the exit status that follows it.
    rc = 0
    p = body.find(needle)
    if p >= 0:
        after = body[p + len(needle):]
        end = after.find("@@")
        digits = after[:end] if end >= 0 else after
        try:
            rc = int(digits.strip())
        except ValueError:
            rc = 0
        body = body[:p]

    # "sh -i" prints its prompt; it can sit on the line before the marker.
    body = body.rstrip()
    while body.endswith("dp#"):
        body = body[:-3].rstrip()
    if body:
        print(body)
    return rc



def main():
    ap = argparse.ArgumentParser(description="persistent shell on the DP over PCIe")
    ap.add_argument("-c", "--command")
    ap.add_argument("-t", "--timeout", type=float, default=30.0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--skip-window", action="store_true")
    a = ap.parse_args()

    try:
        s = Session(program_window=not a.skip_window)
    except Exception as exc:
        print("ffn-dpsh: cannot map the DP window: %s" % exc)
        return 2
    try:
        err = s.check()
        if err:
            print("ffn-dpsh: %s" % err)
            return 2
        if a.status:
            st = s.status()
            print("agent v%(version)d gen=%(gen)d up=%(agent_up)d "
                  "shell_alive=%(shell_alive)d" % st)
            print("  in  %d/%d bytes queued" %
                  ((st["in_head"] - st["in_tail"]) & 0xFFFFFFFF, IN_SIZE))
            print("  out %d/%d bytes pending" %
                  ((st["out_head"] - st["out_tail"]) & 0xFFFFFFFF, OUT_SIZE))
            return 0
        if a.command:
            return one_shot(s, a.command, a.timeout) and 1 or 0
        interactive(s)
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
