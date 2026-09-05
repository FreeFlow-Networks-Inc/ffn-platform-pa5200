#!/bin/bash
# Deliver the DP rootfs over the CP<->DP PCIe network instead of by memory
# staging.
#
# Memory staging cannot work for an image this size:
#   * pre-boot, cvmx_bootmem writes its free-list links INTO free blocks, so the
#     head of the image is always overwritten (proven: every candidate address
#     held a pointer to itself+4MB);
#   * post-boot, the CP reaches DP DRAM only through 4 MB PEM0_BAR1_INDEXn
#     windows -- 16 of them, so the first 64 MB -- and ffn_dpstage.py is fixed
#     to index 1 (0x400000..0x800000).
#
# Over dpnet there is no such limit, and the DP has ~30 GB of RAM so the image
# costs nothing in tmpfs. It is written to /tmp/dproot.sqfs, which is exactly
# where ffn-dproot looks BEFORE trying /dev/mem -- so no change to that script.
set -u
IMG=${1:-/opt/ffn/ffn-dp-buildroot.squashfs}
PORT=${PORT:-8099}
CPIP=127.1.2.1
DPIP=127.1.2.2
say(){ echo "dp-fetch: $*"; }

[ -f "$IMG" ] || { say "no such image: $IMG"; exit 1; }
ip -o addr show ffndp0 2>/dev/null | grep -q "$CPIP" || {
	say "ffndp0 has no $CPIP -- run ffn-dpnet-up6.sh first"; exit 1; }
ping -c1 -W2 "$DPIP" >/dev/null 2>&1 || { say "DP not answering on $DPIP"; exit 1; }

SUM=$(sha256sum "$IMG" | cut -c1-16)
say "image $(stat -c %s "$IMG") bytes, sha256 $SUM"

cd "$(dirname "$IMG")" || exit 1
python3 -m http.server "$PORT" --bind "$CPIP" > /tmp/dpfetch-http.log 2>&1 &
HTTP=$!
trap 'kill $HTTP 2>/dev/null' EXIT
for i in $(seq 1 20); do
	ss -lnt 2>/dev/null | grep -q ":$PORT" && break
	sleep 1
done
ss -lnt 2>/dev/null | grep -q ":$PORT" || { say "server did not come up; log:"; cat /tmp/dpfetch-http.log; exit 1; }
say "serving $(basename "$IMG") on $CPIP:$PORT (pid $HTTP)"

say "DP fetching (19 MB over a polled transport -- expect this to take a while)"
/usr/local/bin/ffn-dpsh -t 900 -c "rm -f /tmp/dproot.sqfs; wget -q -O /tmp/dproot.sqfs http://$CPIP:$PORT/$(basename "$IMG"); echo wget_rc=\$?; ls -l /tmp/dproot.sqfs; sha256sum /tmp/dproot.sqfs | cut -c1-16" 2>&1 | tail -8

say "done; the image is at /tmp/dproot.sqfs on the DP, where ffn-dproot expects it"
