#!/bin/bash
# Give the CP a real shell over pcnet using sshd, replacing the telnetd path.
#
# ffn-cpsh could not work for two independent reasons:
#
#   1. It connects to busybox telnetd on 127.1.1.2:2323, started by
#      /sbin/ffn-cpshd "in the NFS rootfs" -- and the NFS root was not mounted
#      at boot, so nothing ever started it.
#   2. Even mounted, the Buildroot rootfs busybox has NO telnetd applet (nor nc).
#      Only the telnet CLIENT is compiled in. So ffn-cpshd as written cannot run
#      on this rootfs at all.
#
# But the rootfs ships full OpenSSH -- usr/sbin/sshd and usr/bin/ssh -- with the
# sshd privsep user already in /etc/passwd, var/empty present, and an sftp
# subsystem. So use that: encrypted, standard tooling, scp/sftp for free, and no
# custom client. `ssh root@127.1.1.2` replaces `ffn-cpsh`.
#
# All of this is configured from the MP, because the rootfs lives on the MP's
# SSD. SSH key files are portable, so host keys generated here with the MP's
# x86 ssh-keygen are perfectly valid for the CP's mips64 sshd to read.
#
# Root has an empty password field (root:::::::: in shadow), and sshd refuses
# empty-password auth by default, so key authentication is the only sane route
# and PermitRootLogin is set to prohibit-password rather than yes.
#
# ListenAddress is left at the default rather than pinned to 127.1.1.2: sshd
# would fail to bind if it started before pcnet brought ffnnet0 up, and the CP
# holds no IP on eth0/eth1, so the default is already confined to the PCIe link
# plus loopback -- the same isolation boundary ffn_cpsh.py documents.
set -u
R=/opt/ffn-cproot
MARK="# FFN: CP-over-pcnet sshd settings"

echo "=== 1. MP keypair for reaching the CP ==="
if [ -f /root/.ssh/id_ed25519 ]; then
	echo "  /root/.ssh/id_ed25519 already exists, reusing"
else
	mkdir -p /root/.ssh && chmod 700 /root/.ssh
	ssh-keygen -t ed25519 -N "" -C "root@ffn-mp -> CP over pcnet" \
		-f /root/.ssh/id_ed25519 >/dev/null
	echo "  generated /root/.ssh/id_ed25519"
fi

echo "=== 2. CP host keys ==="
mkdir -p "$R/etc/ssh"
for t in ed25519 rsa; do
	f="$R/etc/ssh/ssh_host_${t}_key"
	if [ -f "$f" ]; then
		echo "  ssh_host_${t}_key exists"
	else
		if [ "$t" = rsa ]; then
			ssh-keygen -t rsa -b 3072 -N "" -C "ffn-cp" -f "$f" >/dev/null
		else
			ssh-keygen -t "$t" -N "" -C "ffn-cp" -f "$f" >/dev/null
		fi
		chmod 600 "$f"; chmod 644 "$f.pub"
		echo "  generated ssh_host_${t}_key"
	fi
done

echo "=== 3. authorize the MP key for root on the CP ==="
mkdir -p "$R/root/.ssh"
chmod 700 "$R/root/.ssh"
if grep -qf /root/.ssh/id_ed25519.pub "$R/root/.ssh/authorized_keys" 2>/dev/null; then
	echo "  key already authorized"
else
	cat /root/.ssh/id_ed25519.pub >> "$R/root/.ssh/authorized_keys"
	echo "  appended MP key to authorized_keys"
fi
chmod 600 "$R/root/.ssh/authorized_keys"
chown -R 0:0 "$R/root/.ssh"

echo "=== 4. sshd_config ==="
if grep -q "$MARK" "$R/etc/ssh/sshd_config" 2>/dev/null; then
	echo "  already configured"
else
	cat >> "$R/etc/ssh/sshd_config" <<EOF

$MARK
# Key auth only: root has an empty password field, and sshd rejects
# empty-password logins anyway. prohibit-password allows keys, not passwords.
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
PermitEmptyPasswords no
# Explicit host keys, generated on the MP (key files are portable).
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
# ListenAddress deliberately NOT pinned to 127.1.1.2: sshd would fail to bind if
# it ever started before pcnet brought ffnnet0 up, and the CP holds no IP on
# eth0/eth1, so the default is already confined to the PCIe link and loopback.
EOF
	echo "  appended $MARK block"
fi

echo "=== 5. /sbin/ffn-cpshd, sshd flavour ==="
cat > "$R/sbin/ffn-cpshd" <<'EOF'
#!/bin/sh
# ffn-cpshd -- serve a shell on the CP over the pcnet PCIe link.
#
# Was: busybox telnetd -b 127.1.1.2:2323 -l /bin/bash
# The Buildroot rootfs busybox has no telnetd applet (only the client), so that
# could never run here. This rootfs ships full OpenSSH instead, so use it:
# reach the CP with `ssh root@127.1.1.2` from the MP.
#
# sshd needs /dev/pts for interactive sessions, and the lean initramfs does not
# mount it, so do that first -- same as the telnetd version did.
BB=/bin/busybox
if ! grep -q " /dev/pts " /proc/mounts 2>/dev/null; then
	mkdir -p /dev/pts
	mount -t devpts devpts /dev/pts 2>/dev/null
fi
if $BB pidof sshd >/dev/null 2>&1; then
	echo "ffn-cpshd: sshd already running"
	exit 0
fi
# -e sends the log to stderr so a boot-time failure is visible on the console
# rather than vanishing into a syslog nobody is running.
exec /usr/sbin/sshd -D -e
EOF
chmod 755 "$R/sbin/ffn-cpshd"
chown 0:0 "$R/sbin/ffn-cpshd"
echo "  wrote $R/sbin/ffn-cpshd"

echo "=== done ==="
