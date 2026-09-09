#!/bin/sh
# One-time handoff from the initial Perl run to the full completion pipeline.
set -eu
BASE=/mnt/clones/debian-mips64
while systemctl is-active --quiet ffn-debian-port.service; do sleep 5; done
test "$(cat "$BASE/logs/complete-port.exit")" = 0 || {
    echo 'Initial package run failed; preserving it for diagnosis.' >&2
    exit 1
}
sh "$BASE/setup-port-completion.sh"
exec sh "$BASE/resume-port.sh"
