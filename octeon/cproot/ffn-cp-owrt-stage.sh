#!/bin/bash
# Stage an OpenWrt userland as the OCTEON control plane's NFS root.
#
# Why OpenWrt: the CP is big-endian mips64 n64, which essentially no
# distribution ships. OpenWrt's "mips64_octeonplus" arch is built for exactly
# this CPU -- ELF64, big endian, mips64r2, octeon -- and comes with ~8,500
# prebuilt packages and opkg. Verified against our own userland's ELF header
# before adopting it.
#
# The tree stays PURE MUSL. glibc contributes exactly two files, because
# FFN's own CP binaries need exactly two (readelf: ffn_pcnetd NEEDs only
# libc.so.6, interp /lib64/ld.so.1). Copying the whole glibc runtime in is a
# trap: OpenWrt has /lib64 -> lib, so it lands in /lib, and libgcc_s.so.1
# exists in BOTH trees. The glibc build references _dl_find_object, which musl
# does not provide, so every musl binary in the tree dies at relocation with
#   Error relocating /lib/libgcc_s.so.1: _dl_find_object: symbol not found
# libgcc_s is a compiler runtime rather than libc, which is why it is the one
# name that overlaps. Leave musl's copy alone.
set -euo pipefail

DEST=${DEST:-/opt/ffn-cproot-owrt}
GLIBC_SRC=${GLIBC_SRC:-/opt/ffn-cproot}
MIRROR=${MIRROR:-http://127.1.1.1:8080/openwrt}
TARBALL=${TARBALL:-/root/imagebuilder-octeon/bin/targets/octeon/generic/openwrt-24.10.4-octeon-generic-generic-rootfs.tar.gz}
OWRT_KVER=${OWRT_KVER:-6.6.110}

[ -s "$TARBALL" ] || { echo "no rootfs tarball at $TARBALL" >&2; exit 1; }

echo "== extracting a pristine tree =="
rm -rf "$DEST"; mkdir -p "$DEST"
tar -xzf "$TARBALL" -C "$DEST"
echo "   $(find "$DEST" -type f | wc -l) files, $(du -sh "$DEST" | cut -f1)"

echo "== feeds -> the MP-side cache =="
F=$DEST/etc/opkg/distfeeds.conf
sed -i "s#https://downloads.openwrt.org/#$MIRROR/#g" "$F"
# The CP has no internet path of its own: its transport address is in 127/8 and
# routing that outward would mean relaxing martian-source handling on a
# firewall. The MP proxies instead. Plain HTTP is fine because opkg keeps
# "option check_signature", so indexes are still usign-verified end to end.
sed -i "s#^\(src/gz openwrt_kmods .*\)#\# DISABLED by FFN: these kmods are built for OpenWrt $OWRT_KVER; this CP runs its own 6.18 kernel, so none of them can load.\n\#\1#" "$F"
grep -c "^src/gz" "$F" | sed "s/^/   active feeds: /"
grep -q "^option check_signature" "$DEST/etc/opkg.conf" || echo "option check_signature" >> "$DEST/etc/opkg.conf"

echo "== the two glibc files, and only those =="
for f in ld.so.1 libc.so.6; do
	src="$GLIBC_SRC/lib/$f"
	[ -e "$src" ] || { echo "   MISSING $src" >&2; exit 1; }
	# Refuse to clobber anything musl shipped.
	if [ -e "$DEST/lib/$f" ]; then
		echo "   REFUSING to overwrite $DEST/lib/$f -- musl ships that name" >&2
		exit 1
	fi
	cp -a "$src" "$DEST/lib/$f"
	echo "   + /lib/$f"
done

echo "== FFN CP tooling =="
for f in ffn_pcnetd ffn_nfsmount ffn-nfsroot.sh ffn-cpshd; do
	[ -e "$GLIBC_SRC/sbin/$f" ] && cp -a "$GLIBC_SRC/sbin/$f" "$DEST/sbin/$f" && echo "   + /sbin/$f"
done
[ -d "$GLIBC_SRC/usr/local" ] && cp -a "$GLIBC_SRC/usr/local" "$DEST/usr/" && echo "   + /usr/local"

echo "== ssh access =="
mkdir -p "$DEST/root/.ssh" "$DEST/etc/ssh"; chmod 700 "$DEST/root/.ssh"
for k in "$GLIBC_SRC/root/.ssh/authorized_keys" /root/.ssh/id_ed25519.pub; do
	[ -s "$k" ] && { cat "$k" >> "$DEST/root/.ssh/authorized_keys"; }
done
sort -u -o "$DEST/root/.ssh/authorized_keys" "$DEST/root/.ssh/authorized_keys" 2>/dev/null || true
chmod 600 "$DEST/root/.ssh/authorized_keys" 2>/dev/null || true
# Host keys are plain files and architecture independent, so generate on the MP.
for t in ed25519 rsa; do
	[ -s "$DEST/etc/ssh/ssh_host_${t}_key" ] || ssh-keygen -q -t $t -N "" -f "$DEST/etc/ssh/ssh_host_${t}_key" </dev/null
done
grep -q "^PermitRootLogin" "$DEST/etc/ssh/sshd_config" 2>/dev/null \
	&& sed -i "s/^PermitRootLogin.*/PermitRootLogin prohibit-password/" "$DEST/etc/ssh/sshd_config" \
	|| echo "PermitRootLogin prohibit-password" >> "$DEST/etc/ssh/sshd_config"
echo "   authorized_keys: $(wc -l < "$DEST/root/.ssh/authorized_keys" 2>/dev/null || echo 0) key(s); host keys generated"

echo "== runtime dirs =="
# OpenWrt has /var -> /tmp and leaves procd to create /tmp/{lock,run,log,state}
# during preinit. FFN never runs procd: the CP root is entered by chroot from
# the initramfs, so opkg died on
#   opkg_conf_load: Could not create lock file /var/lock/opkg.lock
# Create them in the tree, and ship a helper to redo it at entry in case /tmp
# is a tmpfs at that point.
for d in lock run log state opkg-lists; do mkdir -p "$DEST/tmp/$d"; done
cat > "$DEST/sbin/ffn-cp-prepare" <<'PREP'
#!/bin/sh
# Recreate what procd would have done, and put the CP on the network.
#
# Nothing runs procd here: ffn-nfsroot.sh chroots straight into this root from
# the initramfs. So the dirs OpenWrt assumes exist have to be made, or opkg
# fails with "Could not create lock file /var/lock/opkg.lock" (/var -> /tmp).
for d in lock run log state opkg-lists; do mkdir -p "/tmp/$d"; done

# sshd's privilege-separation and pidfile dirs. Without these an unattended
# boot reaches a CONSOLE shell only and the CP is unreachable over the PCIe
# link until someone types at the serial port -- which is exactly the manual
# step this whole change exists to remove.
mkdir -p /run/sshd /var/empty
if [ -x /usr/sbin/sshd ] && ! pidof sshd >/dev/null 2>&1; then
	# Host keys are staged by ffn-cp-owrt-stage.sh on the MP; generate only if
	# somehow absent, so a boot never blocks on missing keys.
	[ -s /etc/ssh/ssh_host_ed25519_key ] || \
		ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key </dev/null 2>/dev/null
	/usr/sbin/sshd 2>/tmp/sshd.err && echo "ffn-cp-prepare: sshd started"
fi
exit 0
PREP
chmod +x "$DEST/sbin/ffn-cp-prepare"
echo "   /tmp/{lock,run,log,state,opkg-lists} + /sbin/ffn-cp-prepare"

echo "== export =="
grep -q "^$DEST " /etc/exports || echo "$DEST 127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)" >> /etc/exports
exportfs -ra
echo "staged $DEST"
