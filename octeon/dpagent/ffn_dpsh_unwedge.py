#!/usr/bin/env python3
"""Unwedge the DP's shared mailbox shell. Runs ON THE CP.

WHAT WENT WRONG. ffn-dpsh drives ONE persistent `/bin/sh -i` on the DP through
the PCIe mailbox. A push that was too large for the mailbox got truncated
mid-string, so the shell received an unterminated single quote and dropped to
its continuation prompt. The give-away is a bare ">" at the end of dpsh's
"partial output" dump: everything sent afterwards is consumed as more of that
unfinished string, so every later command appears to time out even when it is
three bytes long.

THE FIX is to close the quote. Sending a lone "'" terminates the string, the
shell then tries to run the accumulated nonsense, fails harmlessly, and
returns to a normal prompt.

This is done by hand here rather than added to ffn_cfgagent because an agent
that automatically injects quote characters into a wedged shell could turn a
truncated command into a DIFFERENT executable command. Recovery of a shared
control channel is an operator action.
"""
import subprocess
import sys

DPSH = "/usr/local/bin/ffn-dpsh"


def dpsh(cmd, timeout="25"):
    p = subprocess.Popen([DPSH, "-c", cmd, "-t", timeout],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = p.communicate()
    return p.returncode, out.decode("utf-8", "replace")


# THE PROBE MUST NOT BE FINDABLE IN THE COMMAND ITSELF.
#
# ffn-dpsh echoes the command back before the reply, so probing with
# `echo PROBE_OK` and testing for "PROBE_OK" in the output matches the ECHO and
# reports a wedged shell as healthy. That is exactly what happened here: this
# script declared "not wedged" and exited without attempting recovery, while
# two independent probes were sitting at a continuation prompt.
#
# So the marker is COMPUTED by the shell and appears nowhere in the text sent.
PROBE_CMD = "expr 123456 + 654321"
PROBE_WANT = "777777"


def alive():
    rc, out = dpsh(PROBE_CMD)
    return PROBE_WANT in out, out


print("=== 1. is it actually wedged? ===")
ok, out = alive()
if ok:
    print("  not wedged -- the shell computed %s" % PROBE_WANT)
    sys.exit(0)
print("  wedged (no computed marker came back)")
tail = out.strip().splitlines()[-1:] if out.strip() else []
print("  last line of partial output: %r" % (tail[0][:70] if tail else ""))

print("=== 2. close the open quote ===")
# A lone single quote. Nothing else: the goal is to terminate the string the
# shell is still reading, not to run anything.
rc, out = dpsh("'")
print("  rc=%s" % rc)

print("=== 3. probe again ===")
for attempt in range(1, 4):
    ok, out = alive()
    if ok:
        print("  recovered on attempt %d" % attempt)
        rc2, out2 = dpsh("rm -f /etc/ffn/dp.env.tmp; expr 11 + 11")
        print("  stale .tmp removed: %s" % ("yes" if "22" in out2 else "unclear"))
        sys.exit(0)
    print("  attempt %d: still no marker" % attempt)

print("  STILL WEDGED. The agent process on the DP has to be restarted, which")
print("  means re-running dpboot8.sh from the CP -- the shell is a child of it.")
sys.exit(1)
