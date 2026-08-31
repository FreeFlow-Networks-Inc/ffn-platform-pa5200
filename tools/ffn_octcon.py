#!/usr/bin/env python3
"""ffn-octcon -- talk to the OCTEON's own u-boot console.

The console is on the host's SECOND 16550A, /dev/ttyS1 @ 115200 8N1. No CPLD
muxing is involved: the Octeon UART is wired straight to it, and it is fully
bidirectional (writing a command echoes and u-boot replies). Discovered by
noticing /proc/tty/driver/serial listed ttyS1 as a real 16550A with tx:0 rx:0 --
a real port nothing had ever used.

Why this matters more than it sounds:

  * It does not depend on PCIe. Every other channel FFN has to the Octeon --
    the 0x6c000 mailbox, the BAR1 DRAM window, the vendor oct-remote-* tools --
    rides the PCIe endpoint. The console survives things that kill that link,
    which is exactly the failure mode that once wedged this host.
  * The mailbox carries commands but returns no output, so FFN could only ever
    report "the bootloader consumed X". Here we get the actual reply.
  * u-boot has read64/read64_csr/write64, md/mw/cp, and octreginfo, so any
    Octeon-visible register -- boot-bus CPLDs, GSER, BGX -- is readable from
    here without the vendor toolchain.

Deliberately NOT a substitute for the mailbox in scripts: the mailbox has a
state machine and defined framing, this is a UART with a prompt. Use this for
interrogation and one-shot commands; use the mailbox for the boot handshake.

For interactive work just use:  picocom -b 115200 /dev/ttyS1   (^A ^X to exit)
Only one reader at a time -- two readers steal each other's bytes.
"""
import argparse
import os
import re
import sys
import termios
import time
import tty

PORT = "/dev/ttyS1"
BAUD = 115200
PROMPT_RE = re.compile(r"\(ram\)#\s*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _configure(fd):
    """115200 8N1, raw, no echo, no flow control."""
    a = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = a
    iflag &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR |
               termios.IGNCR | termios.ISTRIP | termios.BRKINT)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG |
               termios.IEXTEN)
    cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 1          # 0.1 s read timeout
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, termios.B115200,
                       termios.B115200, cc])


DAEMON_PID = "/run/ffn-octeon-console.pid"
DAEMON_LOG = "/var/log/ffn-octeon-console.log"
DAEMON_FIFO = "/run/ffn-octeon-console.in"


def daemon_active():
    """Is ffn_octconsoled holding the port?

    If it is, opening /dev/ttyS1 here would make two readers race for the same
    bytes -- the exact failure that made earlier reads look like the UART was
    dropping characters. So route through the broker's log + FIFO instead.
    """
    try:
        pid = int(open(DAEMON_PID).read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return os.path.exists(DAEMON_FIFO) and os.path.exists(DAEMON_LOG)


def send_via_daemon(cmd, wait=6.0, quiet=0.8):
    """Send through the broker and read the reply out of its log.

    Reads from the log's current end, so we only see output caused by this
    command, not backlog.
    """
    with open(DAEMON_LOG, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = f.tell()
    with open(DAEMON_FIFO, "w") as f:
        f.write(cmd + "\n")

    out = b""
    deadline = time.time() + wait
    last = time.time()
    while time.time() < deadline:
        time.sleep(0.15)
        with open(DAEMON_LOG, "rb") as f:
            f.seek(start)
            new = f.read()
        if len(new) > len(out):
            out = new
            last = time.time()
            txt = out.decode("ascii", "replace").replace("\r", "")
            if PROMPT_RE.search(txt[-40:]):
                break
        elif out and time.time() - last > quiet:
            break

    txt = out.decode("ascii", "replace").replace("\x00", "")
    txt = ANSI_RE.sub("", txt).replace("\r", "")
    lines = [l for l in txt.split("\n")
             if l.strip() and not l.startswith("[in] ") and l.strip() != cmd]
    return "\n".join(lines).strip("\n")


def open_console():
    fd = os.open(PORT, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    os.set_blocking(fd, True)
    _configure(fd)
    return fd


def drain(fd, quiet=0.4, cap=1 << 20):
    """Read until the line goes quiet, discarding. Leaves a clean slate."""
    got = bytearray()
    last = time.time()
    while time.time() - last < quiet and len(got) < cap:
        try:
            b = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            b = b""
        if b:
            got += b
            last = time.time()
        else:
            time.sleep(0.05)
    return bytes(got)


def read_reply(fd, wait=6.0, quiet=0.8, expect_prompt=True, echo_of=None):
    """Collect console output until the prompt, a quiet line, or the deadline.

    Split out of send() so a caller that delivers its command by some OTHER
    channel -- notably the PCI mailbox, which carries commands but returns no
    output -- can still read the reply here. That pairing is the whole point:
    mailbox in, console out.

    echo_of: if given and it appears in the first line, that line is the
    terminal's echo of the command and is dropped.
    """
    out = bytearray()
    deadline = time.time() + wait
    last = time.time()
    while time.time() < deadline:
        try:
            b = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            b = b""
        if b:
            out += b
            last = time.time()
            if expect_prompt and PROMPT_RE.search(
                    out.decode("ascii", "replace").replace("\r", "")[-40:]):
                break
        else:
            if out and time.time() - last > quiet:
                break
            time.sleep(0.05)
    txt = out.decode("ascii", "replace").replace("\x00", "")
    txt = ANSI_RE.sub("", txt).replace("\r", "")
    lines = txt.split("\n")
    if echo_of and lines and echo_of in lines[0]:
        lines = lines[1:]
    return "\n".join(lines).strip("\n")


def send(fd, cmd, wait=6.0, quiet=0.8, expect_prompt=True):
    """Send one command, return its output text.

    Stops on the u-boot prompt when we can see it, otherwise on a quiet line or
    the overall deadline -- some commands (mtest, loop) never return a prompt.
    """
    drain(fd)
    # Two separate traps here.
    #
    # 1. NO leading CR. A bare Enter on an empty line makes u-boot REPEAT the
    #    previous command (its repeatable-command feature), so sending a
    #    newline to "clean the line first" silently re-runs whatever ran last.
    #    That looks exactly like a stale read buffer.
    # 2. The first character after the prompt is sometimes DROPPED by the UART
    #    (seen as "rintenv"/"reeprint"), so send a **leading space**: u-boot
    #    strips leading whitespace, and if a character is going to be eaten it
    #    eats the space instead of the command's first letter. A space is safe
    #    where a CR is not -- it is not an empty line, so it cannot trigger the
    #    repeat behaviour above.
    os.write(fd, b" " + cmd.encode() + b"\r")
    return read_reply(fd, wait=wait, quiet=quiet, expect_prompt=expect_prompt,
                      echo_of=cmd)


def main():
    ap = argparse.ArgumentParser(
        description="run commands on the OCTEON u-boot console (/dev/ttyS1)")
    ap.add_argument("cmd", nargs="*", help="command(s) to run, in order")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="seconds to wait per command (default 6)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="do not stop early on the u-boot prompt")
    ap.add_argument("--raw", action="store_true",
                    help="just listen, print whatever arrives")
    a = ap.parse_args()

    if not os.path.exists(PORT):
        print("no %s on this host" % PORT, file=sys.stderr)
        return 2

    if daemon_active():
        # The broker owns the port. Do NOT open it here.
        if a.raw or not a.cmd:
            print("ffn_octconsoled is running -- follow the shared log:")
            print("  tail -f %s" % DAEMON_LOG)
            return 0
        for c in a.cmd:
            print("=== %s ===" % c)
            print(send_via_daemon(c, wait=a.wait))
            print()
        return 0

    fd = open_console()
    try:
        if a.raw or not a.cmd:
            print("listening on %s @ %d (Ctrl-C to stop)" % (PORT, BAUD))
            try:
                while True:
                    b = os.read(fd, 4096)
                    if b:
                        sys.stdout.write(b.decode("ascii", "replace")
                                         .replace("\x00", ""))
                        sys.stdout.flush()
                    else:
                        time.sleep(0.05)
            except KeyboardInterrupt:
                print()
            return 0

        for c in a.cmd:
            print("=== %s ===" % c)
            print(send(fd, c, wait=a.wait,
                       expect_prompt=not a.no_prompt))
            print()
        return 0

    # unreachable; kept for clarity
    finally:
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
