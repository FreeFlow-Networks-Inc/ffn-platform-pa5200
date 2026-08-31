#!/bin/sh
# Boot the DP with the pty session agent, then prove state PERSISTS across calls:
# a cd in one command must still be in effect in the next, and a shell variable set
# in one call must survive into the following one. That is what v1 could not do.
export OCTEON_PCI_IDS=0x177d0095
export LD_LIBRARY_PATH=/usr/local/lib64
T=/usr/local/cp
E=/sys/bus/pci/devices/0003:03:00.0/enable
D=/dev/shm/ffn
LOG=$D/dpsess.log
K=/tmp/ffn-vmlinux-dp8
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
timeout 25 $T/oct-remote-reset --devnum=1 nowait >/dev/null 2>&1
sleep 2; reen
timeout 120 $T/oct-remote-boot --devnum=1 --loadcache /boot/u-boot-gryphon_dp_etch1_pciboot.bin >/dev/null 2>&1
sleep 20; reen
timeout 150 $T/oct-remote-load --devnum=1 0x21000000 $K >/dev/null 2>&1
echo "  load rc=$?" >> $LOG
reen; wins
python $D/ffn_dpsend.py --wait 25 "bootoctlinux 21000000 numcores=40 console=ttyS0,115200n8 ffn_fdt=0x80000 rw pktbuf=8192,2048 wqe=256,128" >> $LOG 2>&1
sleep 70
i=0
while [ $i -lt 12 ]; do
  OUT=$(/usr/local/bin/ffn-dpsh --status 2>&1)
  echo "  status: $OUT" >> $LOG
  case "$OUT" in *"agent v2"*) break;; esac
  i=$((i + 1)); sleep 5
done
echo "### PERSISTENCE TEST ###" >> $LOG
echo "-- call 1: cd /sbin and set a variable --" >> $LOG
/usr/local/bin/ffn-dpsh -c "cd /sbin; FFNVAR=persisted; pwd" >> $LOG 2>&1
echo "-- call 2: separate invocation; is cwd still /sbin and the var still set? --" >> $LOG
/usr/local/bin/ffn-dpsh -c "pwd; echo VAR=\$FFNVAR" >> $LOG 2>&1
echo "-- call 3: shell identity (same pid == same session) --" >> $LOG
/usr/local/bin/ffn-dpsh -c "echo shell_pid=\$\$; tty" >> $LOG 2>&1
/usr/local/bin/ffn-dpsh -c "echo shell_pid=\$\$; uname -m; nproc" >> $LOG 2>&1
echo "=== DONE ===" >> $LOG
