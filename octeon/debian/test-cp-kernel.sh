#!/bin/bash
# MP-only candidate test using the existing guarded CP boot workflow.
set -eu
K=${1:?usage: test-cp-kernel.sh ABSOLUTE_KERNEL}
sha256sum -c "$K.sha256"
test -s /root/boot618-pcie.sh
test -s /var/lib/ffn-ngfw/octeon/ffn-vmlinux-6.18.49-irqfix
INHIBIT=/run/ffn-cp-health.inhibit
owned=0
if [ ! -e "$INHIBIT" ]; then touch "$INHIBIT"; owned=1; fi
cleanup() { [ "$owned" = 0 ] || rm -f "$INHIBIT"; }
trap cleanup EXIT
BOOT=$(mktemp /var/tmp/ffn-cp-candidate-boot.XXXXXX)
# Select the kernel and reserve the BCM DMA pool in a private copy. The
# known-good default and its reset/BAR-writer guards stay intact for rollback.
python3 - "$K" "$BOOT" <<'PY'
import pathlib, re, shlex, sys
source = pathlib.Path('/root/boot618-pcie.sh').read_text()
source, n = re.subn(r'^K=.*$', 'K=' + shlex.quote(sys.argv[1]), source, count=1, flags=re.M)
assert n == 1
pool = 'ffn_reserve=0x30000000,64M'
if pool not in source:
    anchor = '--extra "ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M"'
    assert source.count(anchor) == 1, 'CP boot arguments changed; inspect before booting'
    source = source.replace(anchor, anchor[:-1] + ' ' + pool + '"')
pathlib.Path(sys.argv[2]).write_text(source)
PY
bash "$BOOT"
cd /opt/ffn-ngfw-v2
bash tools/pcnet-up.sh
for i in $(seq 1 40); do
    if ssh -o BatchMode=yes -o ConnectTimeout=3 \
        -o UserKnownHostsFile="${FFN_CP_KNOWN:-/root/.ssh/known_hosts}" root@127.1.1.2 \
        'uname -r; zcat /proc/config.gz | grep -E "^CONFIG_(CGROUPS|NAMESPACES|NET_NS|USER_NS|NFSD)="; cat /etc/os-release; ls /sys/bus/pci/devices' ; then
        echo 'PASS: CP candidate kernel reachable over PCIe'
        exit 0
    fi
    sleep 3
done
echo 'CP not reachable: inspect console before running the preserved /root/nfsroot_boot.sh rollback.' >&2
exit 1
