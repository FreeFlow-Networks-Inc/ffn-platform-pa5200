#!/bin/sh
# Swap the DP's rootfs from the vendor CentOS tree to the Buildroot one.
#
# MUST run with the session shell in the INITRAMFS, not inside a chroot. The previous
# attempt failed silently because the shell was chrooted into the old CentOS overlay,
# so every absolute path (/mnt/root, /sbin/ffn-dproot, /tmp/dproot.sqfs) resolved
# inside that tree instead of the initramfs. Step 0 asserts we are in the right root.
DPSH=/usr/local/bin/ffn-dpsh
LOG=/dev/shm/ffn/dpswap.log
: > $LOG

echo "### 0. assert we are in the initramfs, not a chroot" >> $LOG
CHK=$($DPSH -t 25 -c "test -f /etc/os-release && echo IN_CHROOT || echo IN_INITRAMFS" 2>&1)
echo "  $CHK" >> $LOG
case "$CHK" in
	*IN_CHROOT*) echo "  ABORT: shell is inside a chroot; send 'exit' first" >> $LOG; exit 1 ;;
esac

echo "### 1. tear down the old CentOS mounts and free its 56MB image" >> $LOG
$DPSH -t 60 -c "umount /mnt/root/proc /mnt/root/sys /mnt/root/dev 2>/dev/null; umount /mnt/root 2>/dev/null; umount /mnt/lower 2>/dev/null; umount /mnt/upper 2>/dev/null; losetup -d /dev/loop0 2>/dev/null; rm -f /tmp/dproot.sqfs; echo torn-down" >> $LOG 2>&1
$DPSH -t 40 -c "mount | grep -cE 'squashfs|overlay'; free | head -2" >> $LOG 2>&1

echo "### 2. install the size-detecting helper into the INITRAMFS /sbin" >> $LOG
B64=$(cat /tmp/ffn-dproot.gz.b64)
LEN=${#B64}
$DPSH -t 25 -c "rm -f /tmp/h2.b64" >/dev/null 2>&1
i=0
while [ $i -lt $LEN ]; do
	CH=$(printf "%s" "$B64" | cut -c$((i + 1))-$((i + 900)))
	$DPSH -t 25 -c "printf '%s' '$CH' >> /tmp/h2.b64" >/dev/null 2>&1
	i=$((i + 900))
done
$DPSH -t 30 -c "base64 -d /tmp/h2.b64 | gunzip > /sbin/ffn-dproot2; chmod 755 /sbin/ffn-dproot2; wc -l < /sbin/ffn-dproot2" >> $LOG 2>&1

echo "### 3. mount the Buildroot rootfs (size read from the squashfs superblock)" >> $LOG
$DPSH -t 240 -c "FFN_DPROOT_NOCHROOT=1 sh /sbin/ffn-dproot2 2>&1" >> $LOG 2>&1

echo "### 4. VERIFY -- this settles the 5.4-headers-on-a-4.9-kernel question" >> $LOG
for c in \
	"chroot /mnt/root /bin/cat /etc/os-release" \
	"chroot /mnt/root /bin/bash --version" \
	"chroot /mnt/root /usr/bin/python3 -V" \
	"chroot /mnt/root /usr/sbin/sshd -V" \
	"chroot /mnt/root /bin/ls --version" \
	"chroot /mnt/root /usr/bin/sort --version" \
	"chroot /mnt/root /usr/bin/uname -srm" ; do
	echo "-- $c" >> $LOG
	$DPSH -t 60 -c "$c 2>&1 | head -2" >> $LOG 2>&1
done
$DPSH -t 40 -c "free | head -2; df -h /mnt/root 2>/dev/null | tail -1" >> $LOG 2>&1
echo "=== DONE ===" >> $LOG
