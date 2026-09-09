#!/bin/sh
# Run on the MP. Direct, key-pinned CP management over PCIe, without a proxy.
set -eu
R=${1:-/opt/ffn-cproot-debian-20260909}
test -s "$R/etc/ssh/ssh_host_ed25519_key.pub"
test -s /root/.ssh/id_ed25519
mkdir -p /etc/ffn-ngfw
awk '{print "ffn-cp-debian " $1 " " $2}' "$R/etc/ssh/ssh_host_ed25519_key.pub" > /etc/ffn-ngfw/cp_known_hosts
cat > /etc/ffn-ngfw/ssh-cp.conf <<'EOF'
Host ffn-cp
    HostName 127.1.1.2
    Port 22
    User root
    IdentityFile /root/.ssh/id_ed25519
    IdentitiesOnly yes
    HostKeyAlias ffn-cp-debian
    UserKnownHostsFile /etc/ffn-ngfw/cp_known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 8
    ServerAliveInterval 15
    ServerAliveCountMax 3
EOF
cat > /usr/local/sbin/ffn-cp <<'EOF'
#!/bin/sh
exec /usr/bin/ssh -F /etc/ffn-ngfw/ssh-cp.conf ffn-cp "$@"
EOF
chmod 755 /usr/local/sbin/ffn-cp
/usr/local/sbin/ffn-cp 'hostname; cat /proc/1/comm; systemctl is-active ffn-sshd'
