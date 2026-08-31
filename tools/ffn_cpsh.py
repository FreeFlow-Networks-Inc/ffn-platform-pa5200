#!/usr/bin/env python3
"""ffn-cpsh -- shell on the OCTEON CP from the MP, over PCIe.

The CP runs busybox telnetd bound to 127.1.1.2:2323 (/sbin/ffn-cpshd in the NFS
rootfs). 127.1.1.2 is the CP's pcnet address, reachable only across the PCIe
link: the CP holds no IP on eth0/eth1, so there is no physical-topology path to
this port. Same isolation boundary the NFS export relies on.

Why this exists: the serial console (/dev/ttyS1) is a single-owner, line-garbling
path shared with the boot log. This gives a clean, concurrent, scriptable shell.

  ffn-cpsh                interactive shell (Ctrl-] detaches)
  ffn-cpsh -c "cmd"       run one command, print output, exit with its status
"""
import argparse
import os
import select
import socket
import sys
import time

HOST, PORT = "127.1.1.2", 2323
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_NAWS = 1, 3, 31


def negotiate(sock, data, want_naws):
    """Strip telnet IAC sequences, answering them. Returns the payload bytes."""
    out = bytearray()
    reply = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        c = data[i + 1]
        if c == IAC:            # escaped 0xff
            out.append(IAC)
            i += 2
            continue
        if c == SB:             # skip subnegotiation up to IAC SE
            j = i + 2
            while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                j += 1
            i = j + 2
            continue
        if i + 2 >= n:
            break
        opt = data[i + 2]
        if c == DO:
            agree = opt == OPT_SGA or (want_naws and opt == OPT_NAWS)
            reply += bytes([IAC, WILL if agree else WONT, opt])
        elif c == DONT:
            reply += bytes([IAC, WONT, opt])
        elif c == WILL:
            agree = opt in (OPT_ECHO, OPT_SGA)
            reply += bytes([IAC, DO if agree else DONT, opt])
        elif c == WONT:
            reply += bytes([IAC, DONT, opt])
        i += 3
    if reply:
        try:
            sock.sendall(bytes(reply))
        except OSError:
            pass
    return bytes(out)


def drain(sock, marker, timeout):
    """Read until marker appears (or timeout). Returns (payload, found)."""
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([sock], [], [], 0.2)
        if not r:
            continue
        data = sock.recv(65536)
        if not data:
            break
        buf += negotiate(sock, data, False)
        if marker and marker in bytes(buf):
            return bytes(buf), True
    return bytes(buf), False


def run_one(sock, cmd, timeout):
    drain(sock, b"", 1.5)                       # let negotiation + prompt settle
    # Kill PTY echo in its OWN line first, so the marker line below is not
    # echoed back at us (otherwise the markers appear twice and framing breaks).
    sock.sendall(b"stty -echo 2>/dev/null\n")
    drain(sock, b"", 0.6)
    sock.sendall(
        ("PATH=/usr/local/cp:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; "
         "echo __FFN_B__; { %s ; } 2>&1; echo __FFN_E__$?\n" % cmd).encode())
    buf, found = drain(sock, b"__FFN_E__", timeout)
    text = buf.decode("utf-8", "replace")
    rc = 0 if found else 124
    if "__FFN_B__" in text:
        text = text.split("__FFN_B__", 1)[1]
    if "__FFN_E__" in text:
        text, tail = text.split("__FFN_E__", 1)
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        rc = int(digits) if digits else 0
    return text.replace("\r\n", "\n").strip("\n"), rc


def interactive(sock):
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        try:
            size = os.get_terminal_size()
            sock.sendall(("stty rows %d cols %d 2>/dev/null\n"
                          % (size.lines, size.columns)).encode())
        except OSError:
            pass
        while True:
            r, _, _ = select.select([fd, sock], [], [])
            if sock in r:
                data = sock.recv(65536)
                if not data:
                    break
                payload = negotiate(sock, data, True)
                if payload:
                    os.write(sys.stdout.fileno(), payload)
            if fd in r:
                ch = os.read(fd, 4096)
                if not ch or b"\x1d" in ch:      # EOF or Ctrl-]
                    break
                sock.sendall(ch.replace(bytes([IAC]), bytes([IAC, IAC])))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def main():
    ap = argparse.ArgumentParser(description="shell on the OCTEON CP over PCIe")
    ap.add_argument("-c", "--command", help="run one command and exit")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("-t", "--timeout", type=float, default=60.0,
                    help="seconds to wait for -c output (default 60)")
    args = ap.parse_args()

    try:
        sock = socket.create_connection((args.host, args.port), timeout=10)
    except OSError as exc:
        sys.stderr.write(
            "ffn-cpsh: cannot reach the CP at %s:%d over PCIe (%s)\n"
            "  check: systemctl is-active ffn-pcnetd   (MP end of the link)\n"
            "         /sbin/ffn-cpshd running on the CP\n" % (args.host, args.port, exc))
        return 2
    sock.settimeout(None)

    if args.command:
        out, rc = run_one(sock, args.command, args.timeout)
        if out:
            print(out)
        return rc

    if not sys.stdin.isatty():
        sys.stderr.write('ffn-cpsh: stdin is not a tty; use -c "command"\n')
        return 2
    sys.stdout.write("ffn-cpsh: CP shell over PCIe (%s:%d) -- Ctrl-] to detach\n"
                     % (args.host, args.port))
    sys.stdout.flush()
    interactive(sock)
    sys.stdout.write("\nffn-cpsh: detached\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
