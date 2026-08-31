#!/bin/sh
# The DP kernel halts entering driver initcalls, right where the OCTEON ethernet
# driver calls cvmx_helper_initialize_packet_io_global(). That call needs its FPA
# pools sized by the pktbuf= / wqe= boot args -- the vendor pulls them from cfgdb
# (cfg.oct.<model>.dp -> fpa.pools[0] and [1]) and FFN hit the identical failure on
# the CP when they were missing. cfgdb is not reachable here, so start from the
# CP's known-good pool sizes. Also pass board_rev, which the vendor sends so the DP
# can tell whether its CP is a 76xx or a 73xx (ours is a CN73XX).
export OCTEON_PCI_IDS=0x177d0095
export LD_LIBRARY_PATH=/usr/local/lib64
T=/usr/local/cp
E=/sys/bus/pci/devices/0003:03:00.0/enable
D=/dev/shm/ffn
LOG=$D/dpboot7.log
K=/boot/vmlinux.oct2-dp
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

CMD="bootoctlinux 21000000 mem=1024 console=ttyS0,115200n8 rw nfsroot=/opt/dpfs,v3 affinity=0 ip=127.1.2.2:127.1.1.1:127.1.2.1:255.255.255.0:octeon:eth0 slot=1 numcores=40 pktbuf=8192,2048 wqe=256,128 board_rev=0.8"
echo "### command is ${#CMD} bytes (mailbox limit is 247)" >> $LOG

sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1
sysctl -w net.ipv4.conf.all.route_localnet=1 >/dev/null 2>&1
ifconfig eth0 down 2>/dev/null; sleep 1
reen
echo "### 1. reset + u-boot" >> $LOG
timeout 25 $T/oct-remote-reset --devnum=1 nowait >> $LOG 2>&1
ifconfig eth0 127.1.2.1 netmask 255.255.255.0 up 2>>$LOG
sleep 2; reen
timeout 120 $T/oct-remote-boot --devnum=1 --loadcache /boot/u-boot-gryphon_dp_etch1_pciboot.bin >> $LOG 2>&1
echo "  uboot rc=$?" >> $LOG
sleep 20; reen
echo "### 2. load the kernel" >> $LOG
timeout 120 $T/oct-remote-load --devnum=1 0x21000000 $K >> $LOG 2>&1
echo "  load rc=$?" >> $LOG
reen; wins
echo "### 3. boot WITH the FPA pool args" >> $LOG
python $D/ffn_dpsend.py --wait 25 "$CMD" >> $LOG 2>&1
echo "### 4. is the DP on the network?" >> $LOG
got=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18; do
  sleep 5
  if ping -c 1 -W 1 127.1.2.2 >/dev/null 2>&1; then
    echo "  *** DP PINGABLE at t+$((i*5))s -- 40-CORE DP IS ON THE NETWORK ***" >> $LOG
    got=1; break
  fi
done
[ $got = 0 ] && echo "  not pingable after 90s" >> $LOG
echo "  arp: $(arp -an 2>/dev/null | grep 127.1.2)" >> $LOG
reen; wins
echo "### 5. how far did the kernel get this time? (tail of printk)" >> $LOG
python $D/ffn_dpmem.py --offset 0x813be8 --length 0x8000 --swap --strings --minlen 6 >> $LOG 2>&1
echo "### cores: $(timeout 12 $T/oct-remote-csr --devnum=1 CIU_PP_RST 2>&1 | head -1)" >> $LOG
echo "=== DONE ===" >> $LOG
