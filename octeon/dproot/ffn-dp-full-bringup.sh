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
#
# Sizes for STAGED PAYLOADS are derived from the payload, not written as
# literals. Those images change size every time they are rebuilt, and a literal
# that stops covering one is the original bug back again, wearing a reservation
# line as camouflage. Fixed-geometry regions stay literal.
mib_up() {                       # bytes of $1 rounded up to whole MiB; rc 1 if unsizable
  __sz=$(wc -c < "$1" 2>/dev/null) || __sz=0
  [ -n "$__sz" ] || __sz=0
  [ "$__sz" -gt 0 ] || return 1
  echo $(( (__sz + 1048575) / 1048576 ))
}
need_mib() {                     # mib_up, but a missing payload stops the script
  __v=$(mib_up "$1") || {
    echo "FATAL: cannot size $1 -- refusing to build a reservation around a" >&2
    echo "       payload that is missing or empty. Stage it first." >&2
    exit 1
  }
  echo "$__v"
}
# Stage a payload the boot line already reserved space for.
#   stage <phys-addr> <path> <reserved-MiB> <timeout>
# Sizing and staging are two reads of the same file some way apart in the script.
# If they ever disagree -- the file was replaced, or a later edit stages a
# different path than the one that was sized -- the reservation ends up SMALLER
# than the blob, which is the original bug with a reservation line vouching for
# it. So the size is re-checked here against what was actually reserved, and a
# mismatch stops the bring-up instead of booting something half-protected.
stage() {
  __now=$(mib_up "$2") || {
    echo "FATAL: $2 is missing at staging time" >&2; exit 1; }
  if [ "$__now" -gt "$3" ]; then
    echo "FATAL: $2 is now ${__now} MiB but the boot line reserved ${3} MiB at $1." >&2
    echo "       Refusing to stage a payload larger than its own reservation." >&2
    exit 1
  fi
  timeout "$4" $T/oct-remote-load --devnum=1 "$1" "$2"
}
# Payload paths, overridable so the A/B verification in DP-MEMORY.md can be run
# against a specific kernel or rootfs image without editing this script.
K=${FFN_DP_KERNEL:-/tmp/ffn-vmlinux-dp10}
IMG=${FFN_DP_IMG:-/tmp/dproot.sqfs}
# 0x400000,4M is the BAR1 window index 1 granularity (each index covers 4 MB),
# which is what the CP can address -- not padding around the 64 KB mailbox ring.
# It also holds the dpnet scratch at 0x500000 and the rings at 0x600000, and ends
# exactly at the kernel link address 0x800000.
#
# The kernel staging area at 0x21000000 is deliberately NOT reserved: bootoctlinux
# copies it to the link address in u-boot, before Linux exists, and nothing reads
# it afterwards. See "considered and deliberately NOT reserved" in DP-MEMORY.md.
# 0x28000000 is different -- ffn-dproot reads it from /dev/mem in USERSPACE, long
# after the allocator is live, which is what makes it load-bearing.
# Size the payloads in the MAIN shell, before the string is built. Calling
# need_mib inline inside ${...:-} would swallow the failure -- a command
# substitution that exits only exits its own subshell, so a missing payload
# would silently yield "0x28000000,M". memparse rejects that and the parser
# then drops that range AND every range after it, which is worse than no
# reservation at all because the boot line still looks correct.
IMG_MIB=$(need_mib "$IMG") || exit 1
# NOT ${FFN_DP_RESERVE:-...}. The CP runs bash 4.2 as /bin/sh, and bash does not
# treat a ";" inside the default word of ${VAR:-...} as literal -- it splits there,
# tries to run the remainder as a command, and leaves you with only the first
# range. dash and busybox ash keep it literal, so this passes every test that is
# not run on the CP. It silently dropped the load-bearing 0x28000000 range on a
# real boot; the kernel could not warn, because it never saw it.
[ -n "$FFN_DP_RESERVE" ] || FFN_DP_RESERVE="0x400000,4M;0x28000000,${IMG_MIB}M"
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
echo "### 1. reset + u-boot" >> $LOG
timeout 25 $T/oct-remote-reset --devnum=1 nowait >/dev/null 2>&1
sleep 2; reen
timeout 120 $T/oct-remote-boot --devnum=1 --loadcache /boot/u-boot-gryphon_dp_etch1_pciboot.bin >/dev/null 2>&1
sleep 20; reen
echo "### 2. stage the rootfs image at 0x28000000 (while u-boot is up)" >> $LOG
stage 0x28000000 "$IMG" "$IMG_MIB" 900 >> $LOG 2>&1
echo "  rootfs load rc=$?" >> $LOG
reen
echo "### 3. stage the kernel" >> $LOG
timeout 200 $T/oct-remote-load --devnum=1 0x21000000 "$K" >/dev/null 2>&1
echo "  kernel load rc=$?" >> $LOG
reen; wins
echo "### 4. boot" >> $LOG
# Log the line verbatim. "cmd N bytes" alone hid a truncated ffn_reserve=
# once already -- the byte count looked plausible for the shorter string.
echo "  bootargs: mem=$FFN_DP_MEM$FFN_DP_RESERVE_ARG" >> $LOG
python $D/ffn_dpsend.py --wait 25 "bootoctlinux 21000000 numcores=40 console=ttyS0,115200n8 ffn_fdt=0x80000 rw mem=$FFN_DP_MEM$FFN_DP_RESERVE_ARG pktbuf=8192,2048 wqe=256,128" >> $LOG 2>&1
sleep 75
echo "### 5. session up?" >> $LOG
i=0
while [ $i -lt 12 ]; do
  OUT=$(/usr/local/bin/ffn-dpsh --status 2>&1)
  case "$OUT" in *"agent v2"*) echo "  $OUT" >> $LOG; break;; esac
  i=$((i + 1)); sleep 5
done
# Boot A of the A/B verification in DP-MEMORY.md runs WITHOUT ffn_reserve=, so
# Linux owns 0x28000000 and may already have overwritten the staged image.
# Mounting it then would feed squashfs a corrupt superblock, which is not a test
# of anything and can take the kernel down. FFN_DP_SKIP_ROOTFS=1 stops after the
# session is up, which is all boot A needs.
if [ "${FFN_DP_SKIP_ROOTFS:-0}" = "1" ]; then
  echo "### 6-8 SKIPPED (FFN_DP_SKIP_ROOTFS=1): session is up, rootfs not mounted" >> $LOG
  /usr/local/bin/ffn-dpsh -t 60 -c "free | head -2" >> $LOG 2>&1
  echo "=== ALLDONE ===" >> $LOG
  exit 0
fi
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
