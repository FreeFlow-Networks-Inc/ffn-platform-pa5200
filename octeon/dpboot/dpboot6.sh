#!/bin/sh
# The fileaddr/filesize coupling did not help, so drop that theory and go back to
# the vendor's EXACT boot arguments -- I had stripped them to "numcores=40", and
# bootoctlinux may well depend on mem= / console= / rw being present (it sizes
# memory and builds the Linux command line from them).
#
# Liveness signal is deliberately independent of my address arithmetic: with the
# full args the kernel configures eth0=127.1.2.2, so a successful boot becomes
# PINGABLE from the CP. That cannot be faked by a wrong DRAM offset.
export OCTEON_PCI_IDS=0x177d0095
export LD_LIBRARY_PATH=/usr/local/lib64
T=/usr/local/cp
E=/sys/bus/pci/devices/0003:03:00.0/enable
D=/dev/shm/ffn
LOG=$D/dpboot6.log
K=/boot/vmlinux.oct2-dp
: > $LOG
reen() { echo 0 > $E 2>/dev/null; sleep 1; echo 1 > $E 2>/dev/null; }
win() {
  timeout 12 $T/oct-remote-csr --devnum=1 PEM0_BAR1_INDEX0 0x1  >/dev/null 2>&1
  timeout 12 $T/oct-remote-csr --devnum=1 PEM0_BAR1_INDEX1 0x11 >/dev/null 2>&1
}

echo "### CP as the DP gateway" >> $LOG
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1
sysctl -w net.ipv4.conf.all.route_localnet=1 >/dev/null 2>&1
ifconfig eth0 down 2>/dev/null; sleep 1
reen
echo "### 1. reset + u-boot (etch1)" >> $LOG
timeout 25 $T/oct-remote-reset --devnum=1 nowait >> $LOG 2>&1
ifconfig eth0 127.1.2.1 netmask 255.255.255.0 up 2>>$LOG
sleep 2; reen
timeout 120 $T/oct-remote-boot --devnum=1 --loadcache /boot/u-boot-gryphon_dp_etch1_pciboot.bin >> $LOG 2>&1
echo "  uboot rc=$?" >> $LOG
sleep 20; reen
echo "  cores: $(timeout 12 $T/oct-remote-csr --devnum=1 CIU_PP_RST 2>&1 | head -1)" >> $LOG
echo "### 2. load the kernel" >> $LOG
timeout 120 $T/oct-remote-load --devnum=1 0x21000000 $K >> $LOG 2>&1
echo "  load rc=$?" >> $LOG
reen; win
echo "### 3. boot with the vendor's EXACT arguments" >> $LOG
python $D/ffn_dpsend.py --wait 25 "bootoctlinux 21000000 mem=1024 console=ttyS0,115200n8 rw nfsroot=/opt/dpfs,v3 affinity=0 ip=127.1.2.2:127.1.1.1:127.1.2.1:255.255.255.0:octeon:eth0 slot=1 numcores=40" >> $LOG 2>&1
echo "  send rc=$?" >> $LOG
echo "### 4. INDEPENDENT liveness: is the DP on the network?" >> $LOG
got=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18; do
  sleep 5
  if ping -c 1 -W 1 127.1.2.2 >/dev/null 2>&1; then
    echo "  *** DP PINGABLE at t+$((i*5))s -- THE 40-CORE OCTEON BOOTED ***" >> $LOG
    got=1; break
  fi
done
[ $got = 0 ] && echo "  not pingable after 90s" >> $LOG
echo "  arp: $(arp -an 2>/dev/null | grep 127.1.2)" >> $LOG
reen
echo "  cores: $(timeout 12 $T/oct-remote-csr --devnum=1 CIU_PP_RST 2>&1 | head -1)" >> $LOG
echo "=== DONE ===" >> $LOG
