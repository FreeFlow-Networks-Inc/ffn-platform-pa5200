#!/bin/sh
# MP-only: install the tested boot/service integration without initiating reset.
# Kernel/root staging and install-mp-cp-access.sh must already be complete.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CP=/opt/ffn-cproot-debian-20260909
DP=/opt/ffn-cproot-owrt/opt/dproot-debian-20260909
test -f "$CP/etc/ssh/ssh_host_ed25519_key.pub"
test -f "$DP/etc/ssh/ssh_host_ed25519_key.pub"
test -s /var/lib/ffn-ngfw/octeon/ffn-vmlinux-systemd-cp-20260909.sha256
mkdir -p /usr/local/libexec/ffn "$CP/usr/local/libexec" \
    "$CP/etc/systemd/system/multi-user.target.wants" \
    /etc/systemd/system/ffn-octeon.service.d /etc/ffn-ngfw
install -m755 "$HERE/test-cp-kernel.sh" /usr/local/libexec/ffn/test-cp-kernel.sh
install -m755 "$HERE/ffn-boot-debian-planes.sh" /usr/local/sbin/ffn-boot-debian-planes
install -m755 "$HERE/ffn-dp-boot.sh" "$CP/usr/local/libexec/ffn-dp-boot.sh"
for unit in ffn-cp-nfs ffn-dpnet ffn-dp-boot; do
    install -m644 "$HERE/$unit.service" "$CP/etc/systemd/system/$unit.service"
done
ln -sf ../ffn-cp-nfs.service "$CP/etc/systemd/system/multi-user.target.wants/ffn-cp-nfs.service"
touch "$DP/etc/ffn-systemd-root"
awk '{print "127.1.1.2 " $1 " " $2}' "$CP/etc/ssh/ssh_host_ed25519_key.pub" > /etc/ffn-ngfw/plane_boot_known_hosts
awk '{print "127.1.2.2 " $1 " " $2}' "$DP/etc/ssh/ssh_host_ed25519_key.pub" >> /etc/ffn-ngfw/plane_boot_known_hosts
cat > /etc/systemd/system/ffn-octeon.service.d/90-debian-planes.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/local/sbin/ffn-boot-debian-planes
TimeoutStartSec=1200
EOF
systemctl daemon-reload
echo 'Installed. Restarting ffn-octeon.service will reset and boot both processors.'
