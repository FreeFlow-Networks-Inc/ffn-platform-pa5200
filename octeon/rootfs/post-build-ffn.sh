#!/bin/sh
# post-build-ffn.sh -- Buildroot post-build hook for FFN's OCTEON planes.
#
# Buildroot 2025.02.9 has no local.mk, and BR2_EXTERNAL is included before
# package/*/*.mk so a package override there loses to the package's own
# definition. BR2_ROOTFS_POST_BUILD_SCRIPT is the one supported seam that runs
# late enough to touch the finished target tree, so FFN's rootfs additions go
# here rather than being patched into upstream package files.
#
# $1 is TARGET_DIR. Buildroot aborts the build on a non-zero exit, which is
# what we want here: unlike the boot-time script, a missing piece at build time
# should stop the image being made rather than ship something half-configured.
set -e

TARGET=$1
[ -n "$TARGET" ] || { echo "post-build-ffn: no TARGET_DIR given" >&2; exit 1; }

HERE=$(dirname "$0")

# 1. FIPS activation script. See ffn-fips.sh for why fipsmodule.cnf cannot be
#    generated here on the x86 build host.
install -D -m 0755 "$HERE/ffn-fips.sh" "$TARGET/sbin/ffn-fips.sh"

# 2. Report on the FIPS provider. Buildroot installs it via OpenSSL's
#    install_modules target when configured with enable-fips; if it is absent
#    the userland is simply not FIPS-capable and saying so here is far better
#    than discovering it on the appliance.
if [ -f "$TARGET/usr/lib/ossl-modules/fips.so" ]; then
	echo "post-build-ffn: FIPS provider present ($(stat -c %s "$TARGET/usr/lib/ossl-modules/fips.so") bytes)"
else
	echo "post-build-ffn: WARNING no usr/lib/ossl-modules/fips.so -- built without enable-fips?" >&2
fi

# 3. The hostname is baked from BR2_TARGET_GENERIC_HOSTNAME, which is "ffn-dp"
#    for this defconfig -- and the CP rootfs is derived from the same build, so
#    the control plane has been calling itself ffn-dp. Two planes answering to
#    one name makes every console log and every ssh prompt ambiguous. Resolve
#    it at boot from the kernel command line instead of baking either name in.
cat > "$TARGET/sbin/ffn-hostname.sh" <<'HOSTEOF'
#!/bin/sh
# Set the hostname from ffn_plane= on the kernel command line. The CP and DP
# share one Buildroot output, so a baked-in name is wrong on one of them.
plane=$(sed -n 's/.*ffn_plane=\([a-z0-9-]*\).*/\1/p' /proc/cmdline 2>/dev/null)
[ -n "$plane" ] || plane=$(cat /etc/hostname 2>/dev/null)
[ -n "$plane" ] || plane=ffn
hostname "ffn-$plane" 2>/dev/null || hostname "$plane" 2>/dev/null
exit 0
HOSTEOF
chmod 0755 "$TARGET/sbin/ffn-hostname.sh"

echo "post-build-ffn: done"
exit 0
