#!/usr/bin/env python3
"""ffn-octconsoled -- one owner for the OCTEON console, many readers.

A UART cannot be shared by two readers: whoever calls read() first takes the
bytes, and the other sees gaps. That is exactly what was happening here -- an
interactive picocom and FFN's tooling both had /dev/ttyS1 open, and the symptom
looked like the port dropping the first character of commands.

So exactly ONE process owns the port: this daemon. Everyone else uses two files.

    read   tail -f /var/log/ffn-octeon-console.log
    write  echo 'ce_cpld_reg display' > /run/ffn-octeon-console.in

Both are plain files, so any number of people and tools can watch at once and
nobody steals anybody's bytes. Writes are serialised through the FIFO in
arrival order.

Usage:
    ffn_octconsoled.py start      # RUNS IN THE FOREGROUND, exclusive on
                                  # /dev/ttyS1 -- it does not fork. Start it as
                                  # ffn-octconsoled.service, or background it
                                  # yourself with setsid. Calling this from a
                                  # script and expecting it to return will hang
                                  # that script (it did exactly that to
                                  # ffn-octeon-up.sh on every cold boot).
    ffn_octconsoled.py stop
    ffn_octconsoled.py status

Commands written to the FIFO get a leading space and a CR appended: u-boot
strips leading whitespace, and a bare CR on an empty line would make it REPEAT
the previous command, which is a trap worth designing out.
"""
import errno
import os
import signal
import sys
import termios
import time

PORT = "/dev/ttyS1"
LOG = "/var/log/ffn-octeon-console.log"
FIFO = "/run/ffn-octeon-console.in"
PIDF = "/run/ffn-octeon-console.pid"


def configure(fd):
    a = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, _i, _o, cc = a
    iflag &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR |
               termios.IGNCR | termios.ISTRIP | termios.BRKINT)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG |
               termios.IEXTEN)
    cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE |
               termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, termios.B115200,
                       termios.B115200, cc])


def holder():
    """pid holding PORT, if any -- so we refuse rather than fight for it."""
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        d = "/proc/%s/fd" % p
        try:
            for f in os.listdir(d):
                try:
                    if os.readlink(os.path.join(d, f)) == PORT:
                        try:
                            comm = open("/proc/%s/comm" % p).read().strip()
                        except OSError:
                            comm = "?"
                        return int(p), comm
                except OSError:
                    pass
        except OSError:
            pass
    return None, None


def running():
    try:
        pid = int(open(PIDF).read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def serve():
    pid, comm = holder()
    if pid:
        print("REFUSING: %s is held by pid %d (%s). Exit it first "
              "(picocom: Ctrl-A Ctrl-X)." % (PORT, pid, comm))
        return 1

    if not os.path.exists(FIFO):
        os.mkfifo(FIFO, 0o600)

    sfd = os.open(PORT, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    os.set_blocking(sfd, True)
    configure(sfd)
    lf = open(LOG, "ab", buffering=0)
    # O_RDWR on a FIFO never sees EOF when writers close, so no reopen dance.
    ffd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)

    open(PIDF, "w").write("%d\n" % os.getpid())
    lf.write(b"\n--- ffn-octconsoled attached ---\n")

    stop = [False]

    def bye(_s, _f):
        stop[0] = True
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    pending = bytearray()
    try:
        while not stop[0]:
            busy = False
            try:
                b = os.read(sfd, 4096)
                if b:
                    lf.write(b)
                    busy = True
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
            try:
                c = os.read(ffd, 4096)
                if c:
                    pending += c
                    busy = True
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
            while b"\n" in pending:
                line, _, rest = bytes(pending).partition(b"\n")
                pending = bytearray(rest)
                cmd = line.strip()
                if cmd:
                    os.write(sfd, b" " + cmd + b"\r")
                    lf.write(b"\n[in] " + cmd + b"\n")
            if not busy:
                time.sleep(0.03)
    finally:
        lf.write(b"\n--- ffn-octconsoled detached ---\n")
        lf.close()
        os.close(sfd)
        os.close(ffd)
        try:
            os.unlink(PIDF)
        except OSError:
            pass
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        if running():
            print("already running (pid %d)" % running())
            return 0
        return serve()
    if cmd == "stop":
        pid = running()
        if not pid:
            print("not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        print("stopped %d" % pid)
        return 0
    pid = running()
    hp, hc = holder()
    print("daemon : %s" % ("running pid %d" % pid if pid else "not running"))
    print("port   : %s" % ("held by pid %d (%s)" % (hp, hc) if hp else "free"))
    print("log    : %s" % LOG)
    print("fifo   : %s" % FIFO)
    print()
    print("read :  tail -f %s" % LOG)
    print("write:  echo 'version' > %s" % FIFO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
