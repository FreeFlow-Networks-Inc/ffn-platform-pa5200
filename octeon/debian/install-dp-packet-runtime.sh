#!/bin/sh
# Install a build artifact for this DP kernel. Recovery loads it on demand.
set -eu
MODULE=${1:?usage: install-dp-packet-runtime.sh path/to/ffn_dp_packet_init.ko}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test "$(id -u)" = 0
case "$(uname -m)" in mips64*) ;; *) echo 'OCTEON MIPS64 dataplane required' >&2; exit 1 ;; esac
RELEASE=$(uname -r)
test "$(modinfo -F name "$MODULE")" = ffn_dp_packet_init
case "$(modinfo -F vermagic "$MODULE")" in "$RELEASE "*) ;; *) echo 'Module kernel version mismatch' >&2; exit 1 ;; esac
DEST=/lib/modules/$RELEASE/extra
install -d "$DEST"
install -m 0644 "$MODULE" "$DEST/ffn_dp_packet_init.ko.new"
mv "$DEST/ffn_dp_packet_init.ko.new" "$DEST/ffn_dp_packet_init.ko"
depmod -a "$RELEASE"
install -m 0755 "$HERE/ffn_dp_packet_init.py" /usr/local/sbin/ffn_dp_packet_init.py
install -m 0644 "$HERE/ffn_dp_boot_health.py" /usr/local/sbin/ffn_dp_boot_health.py
install -m 0644 "$HERE/ffn_dp_link.py" /usr/local/sbin/ffn_dp_link.py
