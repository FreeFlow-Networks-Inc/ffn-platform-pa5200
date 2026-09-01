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

# --- DP memory. Full reasoning in octeon/dproot/DP-MEMORY.md. ---
# The DP has 32 GiB of DRAM. With no mem= the kernel uses its 512 MB default
# (max_memory in arch/mips/cavium-octeon/setup.c) and Linux ends up with ~442 MB.
# mem= is the slice Linux takes OUT of cvmx_bootmem, so it must leave a reserve:
# the FPA pools behind pktbuf=/wqe=, and PKI/SSO/PKO3, are allocated from what is
# left. 30G leaves 2 GiB, about 7x the largest pool set anyone has run here.
FFN_DP_MEM=${FFN_DP_MEM:-30G}
# Physical ranges the MP owns and Linux must not manage. Without these, raising
# mem= puts the DP's only control channel into the buddy allocator.
# REQUIRES a kernel carrying the ffn_reserve patch:
#   strings vmlinux | grep -c ffn_reserve      must be non-zero
#
# Each range is one region with one owner, listed explicitly. A single generous
# block covering all of them would be less arithmetic but worse: its anonymous
# slack is an invitation to drop the next unreviewed thing inside an
# already-reserved range, and the second such user collides with the first
# with the allocator -- the only witness -- already removed.
K=/tmp/ffn-vmlinux-dp8
# This script stages only the kernel, and the kernel staging area is NOT a region
# that needs reserving: bootoctlinux copies it to the link address in u-boot,
# before Linux exists, and nothing reads it afterwards. So the only reservation
# here is the fixed BAR1 window holding the ffn-dpsh mailbox and the dpnet rings,
# which ARE accessed continuously while Linux runs. No derived sizes, hence none
# of the payload-sizing machinery ffn-dp-full-bringup.sh needs for its squashfs.
# NOT ${FFN_DP_RESERVE:-...}. The CP runs bash 4.2 as /bin/sh, and bash does not
# treat a ";" inside the default word of ${VAR:-...} as literal -- it splits there,
# tries to run the remainder as a command, and leaves you with only the first
# range. dash and busybox ash keep it literal, so this passes every test that is
# not run on the CP. It silently dropped the load-bearing 0x28000000 range on a
# real boot; the kernel could not warn, because it never saw it.
[ -n "$FFN_DP_RESERVE" ] || FFN_DP_RESERVE="0x400000,4M"
# Boot A of the A/B verification in DP-MEMORY.md needs the parameter OMITTED, not
# empty: "ffn_reserve=" with nothing after it is a malformed entry, which is a
# different experiment. FFN_DP_RESERVE=none omits it.
# u-boot's command interpreter treats ";" as a COMMAND SEPARATOR. An unescaped
# one truncates the boot line there and takes every argument after it with it --
# silently, because u-boot happily runs the first half and boots. Measured: with
# a bare ";" the kernel received
#   ... rw ffn_reserve=0x400000,4M
# losing the second range AND pktbuf=/wqe=, which are exactly the arguments the
# FPA pools need. Escaping restores the whole line; the kernel still sees a plain
# ";". Done with shell built-ins because the CP's vendor root has no sed.
case "$FFN_DP_RESERVE" in
  none) FFN_DP_RESERVE_ARG= ;;
  *)
    __esc=; __rem=$FFN_DP_RESERVE
    while [ "$__rem" != "${__rem#*;}" ]; do
      __esc="$__esc${__rem%%;*}\;"
      __rem=${__rem#*;}
    done
    FFN_DP_RESERVE_ARG=" ffn_reserve=$__esc$__rem"
    ;;
esac
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
timeout 150 $T/oct-remote-load --devnum=1 0x21000000 "$K" >/dev/null 2>&1
echo "  load rc=$?" >> $LOG
reen; wins
python $D/ffn_dpsend.py --wait 25 "bootoctlinux 21000000 numcores=40 console=ttyS0,115200n8 ffn_fdt=0x80000 rw mem=$FFN_DP_MEM$FFN_DP_RESERVE_ARG pktbuf=8192,2048 wqe=256,128" >> $LOG 2>&1
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
