#!/bin/sh
# Install iproute2 (and busybox as a fallback) into the CP root with APT, so
# runtime dependencies resolve.
#
# Bare `dpkg -i` leaves iproute2 in state "iU" -- unpacked, unconfigured --
# because it Depends on libcap2-bin and dpkg does not fetch dependencies. All
# 11 of iproute2's runtime deps ARE in the local repo (libcap2-bin 1:2.78-1+b1
# included), so apt configures it cleanly. Use apt, not dpkg -i.
set -u
B=/mnt/clones/debian-mips64
C=$B/cproot
R=$B/sid-host/tmp/repo
PORT=8899

pkill -f "http.server $PORT" 2>/dev/null
( cd "$R" && nohup python3 -m http.server "$PORT" --bind 127.0.0.1 \
    >/tmp/repohttp.log 2>&1 & )
sleep 2

for m in proc sys dev; do
	sudo mkdir -p "$C/$m"
	mountpoint -q "$C/$m" || sudo mount --rbind "/$m" "$C/$m"
done
sudo cp /etc/resolv.conf "$C/etc/resolv.conf" 2>/dev/null

CH="sudo chroot $C /usr/bin/env -i LC_ALL=C.UTF-8 LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=dumb DEBIAN_FRONTEND=noninteractive"

echo "=== cproot apt sources ==="
sudo cat "$C/etc/apt/sources.list" 2>/dev/null | sed 's/^/  /'
sudo sh -c "cat $C/etc/apt/sources.list.d/* 2>/dev/null" | sed 's/^/  /'

echo "=== apt-get update ==="
$CH apt-get update -qq 2>&1 | tail -3 | sed 's/^/  /'

echo "=== install iproute2 busybox ==="
$CH apt-get install -y --no-install-recommends iproute2 busybox 2>&1 | tail -8 | sed 's/^/  /'

echo "=== package states (must be ii, not iU) ==="
$CH dpkg -l iproute2 busybox libcap2-bin 2>&1 | tail -4 | sed 's/^/  /'
echo "=== dpkg --audit (must be empty) ==="
out=$($CH dpkg --audit 2>&1)
if [ -z "$out" ]; then echo "  clean"; else echo "$out" | head -6 | sed 's/^/  /'; fi

echo "=== do the binaries run IN the CP root? ==="
printf '  ip -V:      '; $CH ip -V 2>&1 | head -1
printf '  ss -V:      '; $CH ss -V 2>&1 | head -1
printf '  tc -V:      '; $CH tc -V 2>&1 | head -1
printf '  bridge:     '; $CH bridge -V 2>&1 | head -1
printf '  busybox:    '; $CH busybox 2>&1 | head -1
echo "=== ip link (real netlink call) ==="
$CH ip -o link 2>&1 | head -4 | sed 's/^/    /'
echo "=== what the CP root now has for networking ==="
for f in usr/bin/ip usr/sbin/tc usr/bin/ss usr/sbin/bridge usr/bin/busybox sbin/init; do
	if sudo test -e "$C/$f"; then printf '  %-20s present\n' "/$f"; else printf '  %-20s ABSENT\n' "/$f"; fi
done
echo "=== total packages in cproot ==="
$CH dpkg-query -W -f '${Package}\n' 2>/dev/null | wc -l | sed 's/^/  /'
