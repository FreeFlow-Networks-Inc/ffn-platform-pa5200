#!/bin/sh
# VM: make a complete, independently staged root for either processor.
set -eu
BASE=${BASE:-/mnt/clones/debian-mips64}
OUT=${1:?usage: prepare-plane-root.sh ABSOLUTE_NEW_DIRECTORY}
case "$OUT" in "$BASE"/images/plane-*) ;; *) echo 'output must be under images/plane-' >&2; exit 2;; esac
test ! -e "$OUT" || { echo "refusing to overwrite $OUT" >&2; exit 1; }
for item in usr/bin/ip usr/bin/mount usr/bin/python3.14 usr/lib/systemd/systemd usr/lib/systemd/systemd-udevd; do
    test -f "$BASE/cproot/$item"
done
# -x avoids copying the build host's bind-mounted proc/sys/dev into the image.
mkdir -p "$OUT"
cp -ax "$BASE/cproot/." "$OUT/"
# Each deployed plane must get its own identity, not the build chroot's ID.
rm -f "$OUT/etc/machine-id" "$OUT/var/lib/dbus/machine-id"
: > "$OUT/etc/machine-id"
mkdir -p "$OUT/usr/sbin" "$OUT/usr/local/libexec" "$OUT/etc/ssh" \
    "$OUT/etc/systemd/system/multi-user.target.wants" "$OUT/run" "$OUT/oldroot"
install -m755 "$BASE/ssh-build/sshd" "$OUT/usr/sbin/sshd"
mkdir -p "$OUT/usr/libexec"
for bin in sshd-session sshd-auth sftp-server; do
    install -m755 "$BASE/buildroot/build/openssh-10.5p1/$bin" "$OUT/usr/libexec/$bin"
done
for bin in ssh ssh-keygen scp; do install -m755 "$BASE/ssh-build/$bin" "$OUT/usr/bin/$bin"; done
# The unprivileged account is needed by the upstream sshd build. No password
# login is enabled. Device-specific host keys and authorized_keys are added
# on the MP, outside this reusable archive.
grep -q '^sshd:' "$OUT/etc/passwd" || echo 'sshd:x:74:74:sshd:/run/sshd:/usr/sbin/nologin' >> "$OUT/etc/passwd"
grep -q '^sshd:' "$OUT/etc/group" || echo 'sshd:x:74:' >> "$OUT/etc/group"
mkdir -p "$OUT/var/empty" "$OUT/etc/ssh/sshd_config.d"
cat > "$OUT/etc/ssh/sshd_config" <<'EOF'
Port 22
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
HostKey /etc/ssh/ssh_host_ed25519_key
AuthorizedKeysFile .ssh/authorized_keys
Subsystem sftp internal-sftp
SshdSessionPath /usr/libexec/sshd-session
SshdAuthPath /usr/libexec/sshd-auth
Include /etc/ssh/sshd_config.d/*.conf
EOF
cat > "$OUT/etc/systemd/system/ffn-sshd.service" <<'EOF'
[Unit]
Description=FFN SSH access over the plane transport
After=network.target
ConditionPathExists=/etc/ssh/ssh_host_ed25519_key
[Service]
Type=simple
RuntimeDirectory=sshd
ExecStartPre=/usr/sbin/sshd -t
ExecStart=/usr/sbin/sshd -D -e
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
ln -s ../ffn-sshd.service "$OUT/etc/systemd/system/multi-user.target.wants/ffn-sshd.service"
ln -sf /usr/lib/systemd/system/multi-user.target "$OUT/etc/systemd/system/default.target"
# Do not ship the VM's loopback-only package mirror as a usable device feed.
# A deployment supplies its own reachable archive; this file records why.
mkdir -p "$OUT/etc/apt/sources.list.d.disabled"
if [ -f "$OUT/etc/apt/sources.list" ]; then
    mv "$OUT/etc/apt/sources.list" "$OUT/etc/apt/sources.list.d.disabled/build-host.list"
fi
printf '# Configure the MP package mirror before apt update.\n' > "$OUT/etc/apt/sources.list"
chroot "$OUT" /sbin/ldconfig
audit=$(chroot "$OUT" dpkg --audit)
test -z "$audit" || { echo "$audit" >&2; exit 1; }
chroot "$OUT" /usr/lib/systemd/systemd --version
chroot "$OUT" /usr/bin/ip -V
chroot "$OUT" /usr/bin/python3 -c 'import ssl,sqlite3,sys,struct; assert sys.byteorder=="big" and struct.calcsize("P")==8; print(sys.version,ssl.OPENSSL_VERSION,sqlite3.sqlite_version)'
chroot "$OUT" dpkg-query -W > "$OUT/etc/ffn-package-manifest"
cat > "$OUT/etc/ffn-root-status" <<'EOF'
Debian MIPS64 BE n64 staging root; requires the FFN initramfs transport.
Upstream OpenSSH and CPython are locally installed, outside dpkg ownership.
Full boot requires cgroups, a PID 1 handoff, and plane-specific FFN services.
EOF
tar --one-file-system -cpf "$OUT.tar" -C "$OUT" .
sha256sum "$OUT.tar" > "$OUT.tar.sha256"
echo "Verified staging root: $OUT.tar"
