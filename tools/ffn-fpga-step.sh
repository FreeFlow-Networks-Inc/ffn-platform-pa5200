#!/bin/bash
# ffn-fpga-step -- program the CE40 FPGA while the CP sits in u-boot.
#
# Called by ffn-octeon-up.sh and octeon/cp-6.18/boot618-pcie.sh right after
# their reset into u-boot and before the kernel is staged. That is the ONLY
# window: fpga_program exists solely in the CP bootloader.
#
# WHY EVERY BOOT, FORCED (measured on the PA-5220, 2026-10-06)
#   The CA1/CE40 socket is one FPGA and the bitstream decides its PCI id. Any
#   Octeon reset leaves it holding the a101 image with DONE SET, so the old rule
#   "DONE is set, skip the load" left the wrong personality in place on every
#   boot: the socket came up feed:a101, the FE100's register block read
#   all-zero, and nothing downstream could use either. Programming ce40.bin and
#   then booting Linux with NO further reset brings the socket up as feed:a00d
#   (fpga version 19) and the FE100's 5951 registers come alive. So: force the
#   load on every boot, and never reset the Octeon again afterwards.
#
# The work is in tools/ffn_fpga_program.py: stage, sha256 readback, prompt
# wait, console send WITH the ce40= selector, verdict wait. It depends only on
# ffn_octdram and ffn_octboot, which are identical on every deployed tree, so
# this step behaves the same on an MP whose other tooling lags main.
#
# NEVER FATAL. A firewall that boots without the CE40 beats one that does not
# boot, so every path exits 0. The outcome goes to $STATE so the plane status
# layer can report which personality the socket is running.
#
# No backslashes and no line continuations anywhere in this file. The tooling
# that edits these scripts strips one backslash level, which once turned a
# carriage-return strip into deleting every letter r. One command per line.
set -u
STATE=/run/ffn-ce40-personality
cd /opt/ffn-ngfw-v2 || exit 0
FFN_CP_FPGA="${FFN_CP_FPGA:-1}"
if [ "$FFN_CP_FPGA" != 1 ]; then
	echo "--- CE40 FPGA programming SKIPPED (FFN_CP_FPGA=0) ---"
	echo skipped > "$STATE"
	exit 0
fi
echo "--- program the CE40 FPGA (u-boot fpga_program, forced) ---"
# 900 s bounds the prompt wait (90), staging (~60) and the verdict wait (300).
timeout 900 python3 tools/ffn_fpga_program.py
rc=$?
echo "ffn_fpga_program rc=$rc"
if [ "$rc" -eq 0 ]; then
	echo a00d-programmed > "$STATE"
else
	echo "failed rc=$rc" > "$STATE"
	echo "    CE40 NOT programmed: the socket will come up as feed:a101 and the FE100 stays dark"
fi
exit 0
