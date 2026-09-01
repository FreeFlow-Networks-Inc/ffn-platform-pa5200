#!/bin/sh
# FFN: does the BCM88375 write into the DMA region, judged from the KERNEL side?
#
# /dev/mem said the 0xff pattern survived the transfer untouched. But PEM1
# reports bar2_enb=1 (writes are not being UR'd) and bar2_cax=0 (inbound writes
# ARE cached in L2), so a /dev/mem read may simply be looking past where the
# data landed. This runs the same experiment through d->dma_cpu -- the exact
# memory whose bus address the device was handed.
#
# Usage: ffn-dma-kprobe.sh          full cycle: load, fill, run SDK, show
#        ffn-dma-kprobe.sh show     just show
PARAM=/sys/module/ffn_bde/parameters/dma_probe

if [ "$1" = "show" ]; then
	echo show > $PARAM
	dmesg | tail -6
	exit 0
fi

if ! grep -q '^ffn_bde ' /proc/modules; then
	insmod /tmp/ffn_bde.ko || exit 1
fi
[ -c /dev/linux-kernel-bde ] || mknod /dev/linux-kernel-bde c 127 0
[ -c /dev/linux-user-bde ]   || mknod /dev/linux-user-bde   c 126 0

echo fill > $PARAM
echo "--- after fill ---"
dmesg | tail -1

echo "--- running the SDK ---"
cd /usr/share/broadcom || exit 1
printf 'exit\n' > /tmp/bcm-cmds.txt
timeout 240 /usr/local/cp/bcm.user < /tmp/bcm-cmds.txt > /tmp/bcm-run.log 2>&1
grep -E 'Failed accessing|script terminated' /tmp/bcm-run.log

echo "--- after the run, read from the kernel ---"
echo show > $PARAM
dmesg | tail -6
