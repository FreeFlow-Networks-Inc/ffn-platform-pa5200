#!/bin/sh
# Run on the MP; tests the separate Debian SSH servers without any resets.
set -eu
KNOWN=${KNOWN:-/var/tmp/ffn-debian-candidate-known-hosts}
test -s "$KNOWN" || { echo 'Pin the candidate SSH host keys first.' >&2; exit 1; }
probe='set -eu
cat /etc/debian_version
uname -r
test -x /sbin/init
audit=$(dpkg --audit); test -z "$audit" || { echo "$audit"; exit 1; }
ip -V
python3 -c '\''import sys, struct, ssl, sqlite3, decimal, concurrent.futures
assert sys.byteorder == "big" and struct.calcsize("P") == 8
assert sqlite3.connect(":memory:").execute("select 6*7").fetchone() == (42,)
with concurrent.futures.ThreadPoolExecutor(4) as pool:
    assert list(pool.map(abs, [-1,-2,-3,-4])) == [1,2,3,4]
assert decimal.Decimal(1) / 8 == decimal.Decimal("0.125")
print("PASS: BE n64, Python", sys.version.split()[0], ssl.OPENSSL_VERSION, "SQLite, threads, decimal")'\''
/usr/bin/busybox printf "%s %f\n" ok 1.25
echo "PASS: Debian hardware chroot; this is not a PID 1 boot test"
'
echo '=== CP ==='
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN" -p 2222 root@127.1.1.2 "$probe"
echo '=== DP (through the CP) ==='
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN" -J root@127.1.1.2 -p 2222 root@127.1.2.2 "$probe"
