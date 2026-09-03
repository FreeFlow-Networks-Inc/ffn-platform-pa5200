#!/usr/bin/env python3
"""Drive bcm.user on a PTY, streaming every byte to STDOUT as it appears.

Two independent buffering traps have each cost a run:

 1. bcm.user is statically linked, so with stdout on a file or pipe glibc block
    buffers and a wedge loses everything since the last 4-8 KB flush.  A pty
    makes the tty layer line-buffer instead.
 2. Writing the transcript to a file ON THE CP loses it anyway: /tmp is tmpfs
    and /var is a symlink to it, so a CP reset takes the whole log with it.
    Both earlier runs ended with a zero-byte log for exactly this reason.

So the transcript goes to STDOUT unbuffered and is captured on the MP, over the
PCIe pcnet link, by whoever ran the ssh.  Bytes already sent survive the CP
dying mid-run -- which is the whole point.  The CP-side file is kept only as a
convenience copy.

Run it as `python3 -u` so nothing re-buffers on the way out.
"""
import os, pty, select, sys, time

BCM  = "/tmp/dpfs/usr/local/cp/bcm.user"
LOG  = "/tmp/bcm-run.log"
CFG  = "/tmp/bcmcfg"
CMDF = "/tmp/bcm-cmds.txt"
DEADLINE  = float(sys.argv[1]) if len(sys.argv) > 1 else 420.0
SETTLE    = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

out = os.fdopen(1, "wb", buffering=0)
t0  = time.time()

def mark(msg):
    out.write(("### %7.1fs %s\n" % (time.time() - t0, msg)).encode())

for p, what in ((BCM, "bcm.user"), (CFG, "config dir"), (CMDF, "command file")):
    if not os.path.exists(p):
        mark("MISSING %s: %s -- aborting" % (what, p)); sys.exit(2)

cmds = open(CMDF, "rb").read()
mark("start: %s  cfg=%s  deadline=%.0fs settle=%.0fs" % (BCM, CFG, DEADLINE, SETTLE))
mark("commands to send: %r" % cmds)

pid, fd = pty.fork()
if pid == 0:
    os.chdir(CFG)
    os.environ["FFN_BCM_DIR"] = CFG
    os.environ["BCM_CONFIG_FILE"] = CFG + "/config.bcm"
    os.environ["TERM"] = "dumb"
    try:
        os.execv(BCM, ["bcm.user"])
    except Exception:
        pass
    os._exit(127)

mark("forked pid=%d on a pty" % pid)
sent = False
quiet = 0.0
log = open(LOG, "wb", buffering=0)
try:
    while time.time() - t0 < DEADLINE:
        r, _, _ = select.select([fd], [], [], 2.0)
        if r:
            try:
                d = os.read(fd, 4096)
            except OSError:
                mark("pty closed (child gone)"); break
            if not d:
                mark("pty EOF"); break
            out.write(d); log.write(d)
            quiet = 0.0
        else:
            quiet += 2.0
            if quiet >= 30.0:
                mark("no output for %.0fs (still waiting)" % quiet); quiet = 0.0
        if not sent and time.time() - t0 > SETTLE:
            mark("sending command file")
            os.write(fd, cmds if cmds.endswith(b"\n") else cmds + b"\n")
            sent = True
    else:
        mark("DEADLINE reached")
finally:
    st = None
    try:
        os.write(fd, b"exit\n"); time.sleep(1)
    except OSError:
        pass
    try:
        w, st = os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass
    mark("done; child status=%r" % (st,))
    log.close()
