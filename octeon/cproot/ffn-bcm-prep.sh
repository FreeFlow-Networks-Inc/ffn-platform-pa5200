#!/bin/sh
# Runs ON the CP. Idempotent preconditions for a bcm.user run.
#   1. /tmp/dpfs -- vendor firmware is used IN PLACE from the MP's NFS export,
#      never packaged into an FFN image.
#   2. ffn_bcm BEFORE ffn_bde: ffn_bcm is what sets the CMIC enable bit (0 at
#      boot) and keeps the PCI binding; the BDE finds the device by domain/bus.
#   3. Device nodes are not auto-created.
# NOTE the chip RETAINS its PAXB byte order across a CP reboot, so if the BDE
# has run before, ffn_bcm may report ident byte-swapped (0x75830000). Harmless.
set -u
say(){ echo "  prep: $*"; }

grep -q " /tmp/dpfs " /proc/mounts || {
  mkdir -p /tmp/dpfs
  mount -t nfs -o nolock,vers=3,ro 127.1.1.1:/opt/dpfs /tmp/dpfs \
    && say "mounted /tmp/dpfs" || { say "MOUNT FAILED"; exit 2; }
}
[ -x /tmp/dpfs/usr/local/cp/bcm.user ] || { say "bcm.user not executable"; exit 2; }
say "bcm.user $(( $(stat -c %s /tmp/dpfs/usr/local/cp/bcm.user) / 1048576 )) MB"

lsmod | grep -q "^ffn_bcm" || insmod /lib/modules/ffn_bcm.ko || { say "ffn_bcm insmod FAILED"; exit 3; }
lsmod | grep -q "^ffn_bde" || insmod /lib/modules/ffn_bde.ko || { say "ffn_bde insmod FAILED"; exit 3; }
say "modules: $(lsmod | awk 'NR>1{print $1}' | tr '\n' ' ')"

[ -c /dev/linux-kernel-bde ] || mknod /dev/linux-kernel-bde c 127 0
[ -c /dev/linux-user-bde ]   || mknod /dev/linux-user-bde   c 126 0
say "nodes: $(ls /dev/linux-kernel-bde /dev/linux-user-bde /dev/ffn_bcm 2>/dev/null | tr '\n' ' ')"
say "irq: $(grep -E 'ffn_bde|ffn_bcm' /proc/interrupts | tr -s ' ' | sed 's/^ //' || echo none)"

# 4. Transparent huge pages.  bcm.user panicked the 6.18 CP with
#    "Machine Check exception - caused by multiple matching entries in the TLB"
#    at __update_tlb, before printing anything.  The CP kernel is built
#    CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS=y with 4 KB base pages, and bcm.user is
#    a 187 MB static binary, so it faults in a huge number of pages at once; on
#    MIPS a 4 KB entry coexisting with a huge entry for the same VA is exactly
#    that machine check.  Set FFN_BCM_THP=keep to leave it alone and reproduce.
THP=/sys/kernel/mm/transparent_hugepage/enabled
if [ -w "$THP" ]; then
  say "THP was: $(cat $THP)"
  if [ "${FFN_BCM_THP:-never}" = never ]; then
    echo never > "$THP" && say "THP now: $(cat $THP)"
    [ -w /sys/kernel/mm/transparent_hugepage/defrag ] && \
      echo never > /sys/kernel/mm/transparent_hugepage/defrag
  else
    say "THP left as-is (FFN_BCM_THP=${FFN_BCM_THP})"
  fi
else
  say "THP: no sysfs knob (not built in?)"
fi
say "bde dmesg: $(dmesg | grep -c ffn_bde) lines, last: $(dmesg | grep ffn_bde | tail -1)"
