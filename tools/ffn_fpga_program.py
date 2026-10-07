#!/usr/bin/env python3
"""Program the CE40 FPGA from Octeon DRAM and report u-boot's own verdict.

Runs on the MP while the CP sits at its u-boot prompt. Exit status:

    0   the console printed "Full fpga programming SUCCESS"
    1   the console printed a failure, or no verdict arrived in time
    2   could not even try: no bitstream, no console broker, no CSR backend,
        a bad DRAM stage, or no u-boot prompt

Deliberately self-contained. It depends only on ffn_octdram (the BAR1 window)
and ffn_octboot's console primitives, which are identical on every deployed
tree, and NOT on ffn_octctl/ffn_oct, whose versions differ between the MP and
main. The command is a constant, measured on hardware, not built by a helper
that has been wrong about it before.

THE COMMAND, AND WHY EVERY TOKEN IS THERE
    fpga_program load=none ce40=ce40-file addr=400000 size=<hex> force

    load=none      program from DRAM at addr for size bytes; nothing is fetched
    ce40=ce40-file the FPGA SELECTOR, not a filename. The worker at 0xc008e764
                   picks "ce40" only when the parser matched a ce40= token;
                   without it u-boot prints "programming unknown" and HANGS.
                   The value is irrelevant with load=none; the token is not.
    size=          required with load=none (default -1 = "whatever was fetched")
    force          u-boot otherwise skips an FPGA whose DONE is set -- and after
                   any Octeon reset DONE is set with the WRONG (a101) image
                   loaded. Force on every boot.

Sent through the console broker FIFO, not the mailbox: the console is where
the verdict is printed, and the mailbox path has executed this command with a
missing selector before.
"""
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ffn_octdram as od   # noqa: E402
import ffn_octboot as ob   # noqa: E402

BITSTREAM = "/var/lib/ffn-ngfw/vendor/gryphon/ce40.bin"
ADDR = 0x400000            # the vendor's own default; clear of the mailbox and the kernel
PCI = "0000:01:00.0"
PROMPT_WAIT = 90.0
VERDICT_WAIT = 300.0       # u-boot retries 3 times on its own

SUCCESS = "Full fpga programming SUCCESS"
FAILURES = ("Full fpga programming FAILURE", "never asserted", "programming unknown",
            "CE CPLD version check", "CE board power up")


def command(size):
    return "fpga_program load=none ce40=ce40-file addr=%x size=%x force" % (ADDR, size)


def say(msg):
    print("[fpga] " + msg, flush=True)


def main():
    if not os.path.exists(BITSTREAM):
        say("no bitstream at %s" % BITSTREAM)
        return 2
    if not os.path.exists(ob.FIFO):
        say("console broker FIFO %s missing; start ffn-octconsoled" % ob.FIFO)
        return 2
    data = open(BITSTREAM, "rb").read()
    want = hashlib.sha256(data).hexdigest()
    say("bitstream %s  %d bytes  sha256 %s" % (BITSTREAM, len(data), want[:16]))

    # 1. A u-boot prompt first: if the bootloader is not answering there is no
    #    point moving 46 MiB, and a hung u-boot must be reported, not fed.
    deadline = time.time() + PROMPT_WAIT
    ok, why = False, "no poll"
    while time.time() < deadline:
        ok, why = ob.prompt_ok(quiet=True)
        if ok:
            break
        time.sleep(3.0)
    if not ok:
        say("no u-boot prompt (%s); not programming" % why)
        return 2
    say("u-boot prompt: %s" % why)

    # 2. Stage into DRAM and prove the stage by reading it back.
    with od.WindowedDram(PCI) as w:
        if not w.csr.available:
            say("no CSR backend; cannot page the BAR1 window")
            return 2
        nseg = w.write(ADDR, data)
        got = hashlib.sha256(w.read(ADDR, len(data))).hexdigest()
        if got != want:
            say("DRAM readback MISMATCH (%s); refusing to program a bad stage" % got[:16])
            return 2
        say("staged %d segment(s) at 0x%x, readback sha256 matches" % (nseg, ADDR))

    # 3. Send through the console and wait for the bootloader's own verdict.
    cmd = command(len(data))
    start = ob.logsize()
    say("sending: " + cmd)
    ob.fifo(cmd)
    deadline = time.time() + VERDICT_WAIT
    verdict = None
    while time.time() < deadline:
        text = "\n".join(ob.clean(ob.logread(start)))
        if SUCCESS in text:
            verdict = True
            break
        if any(f in text for f in FAILURES):
            verdict = False
            break
        time.sleep(2.0)
    lines = [l for l in ob.clean(ob.logread(start))
             if "fpga" in l.lower() or "programming" in l.lower() or "asserted" in l]
    for l in lines[-6:]:
        say("    " + l)
    if verdict is True:
        say("SUCCESS")
        return 0
    say("FAILURE" if verdict is False else "no verdict within %.0f s" % VERDICT_WAIT)
    return 1


if __name__ == "__main__":
    sys.exit(main())
