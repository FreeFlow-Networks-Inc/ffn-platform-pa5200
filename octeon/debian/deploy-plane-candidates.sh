#!/bin/sh
# MP-only. Stage separate CP and DP Debian roots, then start test SSH servers.
# Does not change either boot root or reset either processor.
set -eu
ARCHIVE=${1:?usage: deploy-plane-candidates.sh ARCHIVE EXPECTED_SHA256}
EXPECTED=${2:?expected SHA256 required}
CP=/opt/ffn-cproot-debian-20260909
DP=/opt/ffn-cproot-owrt/opt/dproot-debian-20260909
KNOWN=/var/tmp/ffn-debian-candidate-known-hosts
test "$(sha256sum "$ARCHIVE" | cut -d' ' -f1)" = "$EXPECTED" || exit 1
test ! -e "$CP" && test ! -e "$DP" || {
    echo 'Candidate roots already exist; inspect them instead of overwriting.' >&2; exit 1;
}
mkdir -p "$CP" "$DP"
tar -xpf "$ARCHIVE" -C "$CP"
cp -a "$CP/." "$DP/"
for root in "$CP" "$DP"; do
    test ! -L "$root/etc/machine-id"
    tr -d '-' < /proc/sys/kernel/random/uuid > "$root/etc/machine-id"
    mkdir -p "$root/root/.ssh" "$root/run/sshd"
    chmod 700 "$root/root/.ssh"
    cp /opt/ffn-cproot-owrt/root/.ssh/authorized_keys "$root/root/.ssh/authorized_keys"
    chmod 600 "$root/root/.ssh/authorized_keys"
    ssh-keygen -q -t ed25519 -N '' -f "$root/etc/ssh/ssh_host_ed25519_key"
done
printf 'ffn-cp-debian\n' > "$CP/etc/hostname"
printf 'ffn-dp-debian\n' > "$DP/etc/hostname"
printf 'ListenAddress 127.1.1.2\n' > "$CP/etc/ssh/sshd_config.d/plane.conf"
printf 'ListenAddress 127.1.2.2\n' > "$DP/etc/ssh/sshd_config.d/plane.conf"
# Pin the keys from the files just generated, before making SSH connections.
awk '{print "[127.1.1.2]:2222 " $1 " " $2}' "$CP/etc/ssh/ssh_host_ed25519_key.pub" > "$KNOWN"
awk '{print "[127.1.2.2]:2222 " $1 " " $2}' "$DP/etc/ssh/ssh_host_ed25519_key.pub" >> "$KNOWN"
mkdir -p /etc/exports.d
printf '%s 127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check)\n' "$CP" > /etc/exports.d/ffn-debian-candidate.exports
exportfs -ra
ssh -o BatchMode=yes root@127.1.1.2 'sh -s' <<'CP_SCRIPT'
set -eu
mkdir -p /mnt/debian-candidate
mount -t nfs -o nolock,vers=3,proto=tcp 127.1.1.1:/opt/ffn-cproot-debian-20260909 /mnt/debian-candidate
for d in proc sys dev; do mount --bind /$d /mnt/debian-candidate/$d; done
chroot /mnt/debian-candidate /usr/sbin/sshd -t
chroot /mnt/debian-candidate /usr/sbin/sshd -p 2222 -E /var/log/ffn-sshd-test.log -o PidFile=/run/sshd-candidate.pid
# Refuse an fsid collision; the existing production DP export uses fsid=7.
if grep -q 'fsid=8' /etc/exports && ! grep -q '^/opt/dproot-debian-20260909 ' /etc/exports; then
    echo 'fsid=8 already assigned; choose another before exporting' >&2; exit 1
fi
grep -q '^/opt/dproot-debian-20260909 ' /etc/exports || \
    echo '/opt/dproot-debian-20260909 127.1.0.0/16(rw,sync,no_root_squash,no_subtree_check,fsid=8)' >> /etc/exports
exportfs -ra
ffn-dpsh -t 30 -c 'set -e; mkdir -p /mnt/debian-candidate; mount -t nfs -o nolock,vers=3,proto=tcp 127.1.2.1:/opt/dproot-debian-20260909 /mnt/debian-candidate; for d in proc sys dev; do mount --bind /$d /mnt/debian-candidate/$d; done; chroot /mnt/debian-candidate /usr/sbin/sshd -t; chroot /mnt/debian-candidate /usr/sbin/sshd -p 2222 -E /var/log/ffn-sshd-test.log -o PidFile=/run/sshd-candidate.pid'
CP_SCRIPT
echo 'Run check-plane-candidates.sh on the MP to verify both hardware sessions.'
