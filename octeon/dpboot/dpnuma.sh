#!/bin/sh
# Boot FFN's NUMA-enabled kernel on the 40-core DP. The previous image panicked
# with "Must build kernel with CONFIG_NUMA for multi-node system." -- that check
# lives inside #ifndef CONFIG_NUMA in arch/mips/cavium-octeon/setup.c and fires when
# the coremask's last core >= CVMX_COREMASK_MAX_CORES_PER_NODE. Rebuilt with
# CONFIG_NUMA=y (panic compiled out, verified absent from the image) and
# CONFIG_NR_CPUS raised 32 -> 48, since 32 could never host 40 cores.
export OCTEON_PCI_IDS=0x177d0095
export LD_LIBRARY_PATH=/usr/local/lib64
T=/usr/local/cp
E=/sys/bus/pci/devices/0003:03:00.0/enable
D=/dev/shm/ffn
LOG=$D/dpnuma.log
K=/tmp/ffn-vmlinux-numa
: > $LOG
reen() { echo 0 > $E 2>/dev/null; sleep 1; echo 1 > $E 2>/dev/null; }
wins() {
  n=0
  while [ $n -lt 16 ]; do
    v=$(printf "0x%x" $(( (n << 4) | 1 )))
    timeout 12 $T/oct-remote-csr --devnum=1 PEM0_BAR1_INDEX$n $v >/dev/null 2>&1
    n=$((n + 1))
  done
}
echo "### kernel: $K ($(wc -c < $K) bytes)" >> $LOG
reen
echo "### 1. reset + u-boot" >> $LOG
timeout 25 $T/oct-remote-reset --devnum=1 nowait >> $LOG 2>&1
sleep 2; reen
timeout 120 $T/oct-remote-boot --devnum=1 --loadcache /boot/u-boot-gryphon_dp_etch1_pciboot.bin >> $LOG 2>&1
echo "  uboot rc=$?" >> $LOG
sleep 20; reen
echo "### 2. load it" >> $LOG
timeout 150 $T/oct-remote-load --devnum=1 0x21000000 $K >> $LOG 2>&1
echo "  load rc=$?" >> $LOG
reen; wins
echo "### 3. boot (initramfs has busybox now, so init can reach a shell)" >> $LOG
python $D/ffn_dpsend.py --wait 25 "bootoctlinux 21000000 mem=1024 console=ttyS0,115200n8 rw numcores=40 pktbuf=8192,2048 wqe=256,128" >> $LOG 2>&1
echo "### 4. wait, then locate and read its log" >> $LOG
sleep 60; reen; wins
python $D/ffn_dpfind.py "Linux version" "CONFIG_NUMA" "Freeing unused" "Kernel panic" "FFN>" >> $LOG 2>&1
echo "### cores: $(timeout 12 $T/oct-remote-csr --devnum=1 CIU_PP_RST 2>&1 | head -1)" >> $LOG
echo "=== DONE ===" >> $LOG
