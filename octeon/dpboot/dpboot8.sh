#!/bin/bash
# dpboot8 -- boot the CN78XX dataplane from a CP running FFN's own 6.18 kernel.
#
# dpboot6/7 and ffn-dp-full-bringup.sh all assume the 4.9 CP inside the vendor
# CentOS root. Four things differ here, each of which silently breaks them:
#
#  1. THE VENDOR TOOLS SIGBUS UNDER THEIR OWN glibc. /opt/dpfs's glibc is 2.16
#     and dies in its own libpthread init before main:
#       SIGBUS in __pthread_initialize_minimal_internal () at nptl-init.c:309
#     glibc 2.34+ folded libpthread/librt into libc and left STUBS, so running
#     the same binaries under the CP's glibc 2.41 loader has nothing left to
#     fault in. That is the whole fix -- the vendor code itself is fine.
#
#  2. THE DP IS NOT --devnum=1 ANY MORE. --devnum indexes the Octeon devices
#     liboct-remote_cp finds in /proc/bus/pci/devices, whose flat format drops
#     the PCI domain. The CP's own three root ports (177d:9700, one per PEM)
#     now enumerate ahead of the DP, so the DP sits at index 3, not 1. Indices
#     move with enumeration, so this DISCOVERS it and then proves it with a
#     register read rather than trusting either number.
#
#  3. THERE IS NO eth0 ON THIS CP, only ffnnet0 (the MP link). dpboot7's
#     `ifconfig eth0 down` / `ifconfig eth0 127.1.2.1` are no-ops here. The CP
#     end of 127.1.2.x comes from dpnet2's ffn_dpnetd TAP instead, which is a
#     separate step after the DP is up.
#
#  4. mem=1024 IS ~1 KiB. memparse() reads a suffix-less number as BYTES, so
#     dpboot7's vendor-derived mem=1024 is not 1 GiB. 30G is the reviewed value
#     (mem= is the slice Linux takes OUT of cvmx_bootmem; 30G of 32 GiB leaves
#     2 GiB for the FPA pools behind pktbuf=/wqe= and PKI/SSO/PKO3).
#
# This run deliberately does NOT stage the squashfs rootfs. The DP kernel
# carries its own initramfs, and 0x28000000 can collide with a 30 GiB kernel's
# managed RAM. Boot to initramfs first, prove the control channel, then deal
# with the rootfs as a separate reviewed step.
set -u

VT=${VT:-/tmp/dpfs}                       # vendor tree, NFS-mounted, used in place
T=$VT/usr/local/cp                        # vendor oct-remote-* (never copied into FFN's root)
UBOOT=$VT/boot/u-boot-gryphon_dp_etch1_pciboot.bin
FFN=/opt/ffn
K=${FFN_DP_KERNEL:-$FFN/ffn-vmlinux-dp-mem}
E=/sys/bus/pci/devices/0003:03:00.0/enable
LOG=${LOG:-/tmp/dpboot8.log}

MEM=${FFN_DP_MEM:-30G}
# One range, and deliberately only one: 0x400000,4M is the BAR1 window index
# granularity the CP can address, and it covers the 64 KB agent mailbox at
# 0x400000, the dpnet scratch at 0x500000 and the rings at 0x600000, ending
# exactly at the kernel link address 0x800000. Raising mem= without this puts
# the DP's only control channel into the buddy allocator.
# A single range also sidesteps BOTH ';' traps: ';' is u-boot's command
# separator (it would truncate the boot line there and boot the first half),
# and bash splits on it inside ${VAR:-a;b} as well.
RESERVE=${FFN_DP_RESERVE:-0x400000,4M}

# Optional squashfs rootfs, staged into DP DRAM at 0x28000000 while u-boot is
# STILL UP. That ordering is load-bearing: oct-remote-load's final
# "setenv fileaddr" step talks to the BOOTLOADER mailbox, so staging after
# Linux is running returns rc=255 even though the bytes transfer -- a false
# failure that invites re-staging over a good copy. Its range gets its OWN
# ffn_reserve: without that, mem=30G means Linux manages 0x28000000 and the
# allocator hands out the staged image.
ROOTFS=${FFN_DP_ROOTFS:-}
ROOTFS_ADDR=0x28000000

say(){ echo "dpboot8: $*" | tee -a "$LOG"; }
: > "$LOG"

# --- 0. preconditions --------------------------------------------------------
grep -q " $VT " /proc/self/mounts || {
  say "mounting the vendor tree read-only"
  mkdir -p "$VT"
  /sbin/ffn_nfsmount 127.1.1.1:/opt/dpfs "$VT" \
    nolock,vers=3,addr=127.1.1.1,proto=tcp,mountproto=tcp,hard,ro >/dev/null 2>&1
}
for f in "$T/oct-remote-csr" "$T/oct-remote-reset" "$T/oct-remote-boot" "$T/oct-remote-load" "$UBOOT" "$K" "$FFN/ffn_dpsend.py"; do
  [ -s "$f" ] || { say "FATAL: missing $f"; exit 1; }
done
# The bring-up script's own precondition: without the ffn_reserve patch the
# boot line's reservation is silently ignored and mem=30G swallows the mailbox.
if [ "$(strings -a "$K" 2>/dev/null | grep -c ffn_reserve)" -eq 0 ]; then
  say "FATAL: $K has no ffn_reserve support; mem=$MEM would put the control channel in the allocator"
  exit 1
fi
say "kernel  $K ($(wc -c < "$K") bytes, ffn_reserve OK)"
say "u-boot  $UBOOT"
RES_ARGS="ffn_reserve=$RESERVE"
if [ -n "$ROOTFS" ]; then
	[ -s "$ROOTFS" ] || { say "FATAL: rootfs $ROOTFS missing"; exit 1; }
	# squashfs 4.0 starts with "hsqs". Refuse anything else rather than stage
	# the wrong thing and have ffn-dproot fail on the far side, where the only
	# diagnostic is a mailbox shell.
	MAG=$(dd if="$ROOTFS" bs=4 count=1 2>/dev/null)
	[ "$MAG" = "hsqs" ] || { say "FATAL: $ROOTFS is not squashfs (magic '$MAG')"; exit 1; }
	# Size the reservation from the FILE, never a literal: the image changes size
	# on every rebuild, and a stale literal is the original bug back again
	# wearing a reservation line as camouflage.
	RFS_B=$(wc -c < "$ROOTFS"); RFS_MIB=$(( (RFS_B + 1048575) / 1048576 ))
	RES_ARGS="$RES_ARGS ffn_reserve=$ROOTFS_ADDR,${RFS_MIB}M"
	say "rootfs  $ROOTFS ($RFS_B bytes -> reserving ${RFS_MIB}M at $ROOTFS_ADDR)"
fi

# --- 1. the modern-loader wrapper -------------------------------------------
LD=/lib/ld.so.1
LP=/lib:/lib64:$VT/usr/local/lib64
OCT(){ "$LD" --library-path "$LP" "$T/$1" "${@:2}"; }

# --- 2. which index is the DP? ----------------------------------------------
guess_devnum(){
  awk '$2 ~ /^177d/ { if ($2 == "177d0095") { print n; exit } n++ }' /proc/bus/pci/devices
}
DEV=${FFN_DP_DEVNUM:-$(guess_devnum)}
[ -n "${DEV:-}" ] || DEV=3
probe(){ OCT oct-remote-csr --devnum="$1" CIU_PP_RST 2>&1 </dev/null | sed 's/\x1b\[[0-9;]*m//g'; }
OUT=$(probe "$DEV")
case "$OUT" in
  *CIU_PP_RST*) say "DP at --devnum=$DEV (discovered): $OUT" ;;
  *)
    say "devnum=$DEV did not answer, sweeping"
    FOUND=
    for n in 0 1 2 3 4 5 6 7; do
      O=$(probe "$n"); case "$O" in *CIU_PP_RST*) FOUND=$n; OUT=$O; break;; esac
    done
    [ -n "$FOUND" ] || { say "FATAL: no devnum reached the DP"; exit 1; }
    DEV=$FOUND; say "DP at --devnum=$DEV (swept): $OUT"
    ;;
esac

# --- 3. the sequence --------------------------------------------------------
# reen() re-enables the endpoint after each vendor step: the vendor tools drop
# the endpoint's BARs behind them. Safe here only because nothing else is
# touching this endpoint -- toggling enable underneath a live ffn-dpsh session
# or a BAR poller is what produces the AER storms.
reen(){ echo 0 > "$E" 2>/dev/null; sleep 1; echo 1 > "$E" 2>/dev/null; }
wins(){ n=0; while [ $n -lt 16 ]; do
          OCT oct-remote-csr --devnum="$DEV" "PEM0_BAR1_INDEX$n" "$(printf '0x%x' $(( (n << 4) | 1 )))" >/dev/null 2>&1
          n=$((n + 1)); done; }

reen
say "1. reset the DP"
OCT oct-remote-reset --devnum="$DEV" nowait >>"$LOG" 2>&1; say "   reset rc=$?"
sleep 2; reen

say "2. load DP u-boot into L2 cache"
OCT oct-remote-boot --devnum="$DEV" --loadcache "$UBOOT" >>"$LOG" 2>&1; say "   u-boot rc=$?"
sleep 20; reen

if [ -n "$ROOTFS" ]; then
	say "2b. stage the rootfs at $ROOTFS_ADDR (u-boot still up)"
	OCT oct-remote-load --devnum="$DEV" "$ROOTFS_ADDR" "$ROOTFS" >>"$LOG" 2>&1
	say "    rootfs rc=$?"
	reen
fi
say "3. stage the kernel at 0x21000000"
OCT oct-remote-load --devnum="$DEV" 0x21000000 "$K" >>"$LOG" 2>&1; say "   kernel rc=$?"
reen; wins

say "4. boot (mem=$MEM $RES_ARGS)"
/usr/bin/python3 "$FFN/ffn_dpsend.py" --wait 25 \
  "bootoctlinux 21000000 numcores=40 console=ttyS0,115200n8 ffn_fdt=0x80000 rw mem=$MEM $RES_ARGS pktbuf=8192,2048 wqe=256,128" \
  >>"$LOG" 2>&1
say "   dpsend rc=$?"

say "5. waiting for the DP agent session"
i=0
while [ $i -lt 18 ]; do
  S=$(/usr/local/bin/ffn-dpsh --status 2>&1)
  case "$S" in *"agent v2"*) say "   UP: $S"
    # The kernel logs one line per range it excludes. This is the only
    # trustworthy check that a REPEATED ffn_reserve= ACCUMULATED rather than
    # the last occurrence overwriting the earlier one: if just one range
    # appears here, the other is NOT protected and the staged image will be
    # handed out by the allocator.
    say "   ranges the DP kernel kept out of its memory map:"
    /usr/local/bin/ffn-dpsh -t 60 -c "dmesg | grep ffn_reserve" 2>&1 | grep -oE "0x[0-9a-f]+\+0x[0-9a-f]+.*" | sed "s/^/     /" | tee -a "$LOG"
    say "=== DP IS ALIVE ==="; exit 0;; esac
  i=$((i + 1)); sleep 5
done
say "   no agent session after 90s; last status: ${S:-none}"
say "   (kernel may still be up with no agent -- check with ffn_dplog.py)"
exit 2
