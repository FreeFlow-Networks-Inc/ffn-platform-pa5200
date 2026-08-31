#!/bin/sh
# Full end-to-end DP bring-up: stage the rootfs image, boot, then mount it.
#
# Order matters. The squashfs is staged BEFORE the kernel boots, because
# oct-remote-load talks to the bootloader mailbox for its final "setenv fileaddr"
# step -- with Linux already running that step fails (rc=255) even though the data
# transfers fine. Staging first keeps the return code meaningful.
export OCTEON_PCI_IDS=0x177d0095
export LD_LIBRARY_PATH=/usr/local/lib64
T=/usr/local/cp
E=/sys/bus/pci/devices/0003:03:00.0/enable
D=/dev/shm/ffn
LOG=$D/dpfull3.log
K=/tmp/ffn-vmlinux-dp10
IMG=/tmp/dproot.sqfs
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
reen
echo "### 1. reset + u-boot" >> $LOG
timeout 25 $T/oct-remote-reset --devnum=1 nowait >/dev/null 2>&1
sleep 2; reen
timeout 120 $T/oct-remote-boot --devnum=1 --loadcache /boot/u-boot-gryphon_dp_etch1_pciboot.bin >/dev/null 2>&1
sleep 20; reen
echo "### 2. stage the 55MB rootfs image at 0x28000000 (while u-boot is up)" >> $LOG
timeout 900 $T/oct-remote-load --devnum=1 0x28000000 $IMG >> $LOG 2>&1
echo "  rootfs load rc=$?" >> $LOG
reen
echo "### 3. stage the kernel" >> $LOG
timeout 200 $T/oct-remote-load --devnum=1 0x21000000 $K >/dev/null 2>&1
echo "  kernel load rc=$?" >> $LOG
reen; wins
echo "### 4. boot" >> $LOG
python $D/ffn_dpsend.py --wait 25 "bootoctlinux 21000000 numcores=40 console=ttyS0,115200n8 ffn_fdt=0x80000 rw pktbuf=8192,2048 wqe=256,128" >> $LOG 2>&1
sleep 75
echo "### 5. session up?" >> $LOG
i=0
while [ $i -lt 12 ]; do
  OUT=$(/usr/local/bin/ffn-dpsh --status 2>&1)
  case "$OUT" in *"agent v2"*) echo "  $OUT" >> $LOG; break;; esac
  i=$((i + 1)); sleep 5
done
echo "### 6. mount the big rootfs (baked-in helper, no shipping needed)" >> $LOG
/usr/local/bin/ffn-dpsh -t 240 -c "FFN_DPROOT_NOCHROOT=1 sh /sbin/ffn-dproot 2>&1" >> $LOG 2>&1
echo "### 7. prove it" >> $LOG
/usr/local/bin/ffn-dpsh -t 60 -c "chroot /mnt/root /bin/cat /etc/redhat-release" >> $LOG 2>&1
/usr/local/bin/ffn-dpsh -t 60 -c "chroot /mnt/root /bin/bash --version" >> $LOG 2>&1
/usr/local/bin/ffn-dpsh -t 60 -c "free | head -2" >> $LOG 2>&1
echo "=== DONE ===" >> $LOG
echo "### 8. BURST test -- this is what used to drop the marker" >> $LOG
/usr/local/bin/ffn-dpsh -t 120 -c "chroot /mnt/root /bin/ls -laR /usr/bin | wc -l" >> $LOG 2>&1
/usr/local/bin/ffn-dpsh -t 120 -c "chroot /mnt/root /bin/cat /etc/redhat-release" >> $LOG 2>&1
/usr/local/bin/ffn-dpsh -t 120 -c "chroot /mnt/root /bin/ls /usr/bin | wc -l" >> $LOG 2>&1
/usr/local/bin/ffn-dpsh -t 60 -c "free | head -2" >> $LOG 2>&1
echo "=== ALLDONE ===" >> $LOG
