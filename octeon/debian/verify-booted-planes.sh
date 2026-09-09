#!/bin/sh
# MP hardware verification after the saved ffn-octeon startup completes.
set -eu
PROBE='set -eu
test "$(readlink /proc/1/exe)" = /usr/lib/systemd/systemd
systemctl is-system-running --wait
systemctl is-active ffn-sshd
audit=$(dpkg --audit); test -z "$audit" || { echo "$audit"; exit 1; }
hostname
uname -r
findmnt -n -o SOURCE,FSTYPE /
printf "CPU cores: "; grep -c ^processor /proc/cpuinfo
printf "Packages: "; dpkg-query -W | wc -l
python3 -c '\''import sys, struct, sqlite3, ssl, concurrent.futures, decimal
assert sys.byteorder == "big" and struct.calcsize("P") == 8
assert sqlite3.connect(":memory:").execute("select 6*7").fetchone() == (42,)
with concurrent.futures.ThreadPoolExecutor(4) as pool:
    assert list(pool.map(abs, [-1,-2,-3,-4])) == [1,2,3,4]
assert decimal.Decimal(1)/8 == decimal.Decimal("0.125")
print("PASS: BE n64, SQLite, threads, decimal,", ssl.OPENSSL_VERSION)'\''
systemctl --failed --no-pager
'
echo '=== MP direct management of CP ==='
/usr/local/sbin/ffn-cp "$PROBE"
/usr/local/sbin/ffn-cp 'systemctl is-active ffn-cp-nfs ffn-dp-boot ffn-dpnet; test "$(grep -c ^processor /proc/cpuinfo)" = 8'
echo '=== DP management through CP ==='
ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts \
    -o 'ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp' \
    root@127.1.2.2 "date -s @$(date +%s) >/dev/null
$PROBE
test \"\$(grep -c ^processor /proc/cpuinfo)\" = 40"
echo 'PASS: both PA-5220 processors booted Debian; direct MP-to-CP management verified.'
