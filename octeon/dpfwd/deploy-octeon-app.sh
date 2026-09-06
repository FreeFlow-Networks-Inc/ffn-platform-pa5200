#!/bin/sh
# Push a binary from the CP into the DP's filesystem, over PCIe. Runs ON THE CP.
#
#   deploy-octeon-app.sh /tmp/ffn-dp-octeon [/tmp/dpb]
#
# WHY CHUNKED. ffn_dpstage.py writes into a 1 MB staging window at 0x500000 --
# everything above it is the ffn-dpsh mailbox and the dpnet rings, and the tool
# refuses to cross that boundary. The dataplane binary is ~3.5 MB. So it goes a
# megabyte at a time: stage, have the DP read that megabyte out of /dev/mem and
# append it, repeat.
#
# THE TRAP THIS SCRIPT EXISTS TO PREVENT. The chain is workstation -> MP -> CP
# -> DP, and the chunking reads the CP's copy. Copying a fresh build to the MP
# and forgetting the MP->CP hop leaves a STALE binary on the CP, which stages
# and verifies perfectly -- every sha256 matches, because they are all matching
# the wrong file. That cost a full deploy-and-run cycle chasing a fix that was
# never in the binary under test. Hence: this prints the source hash first, and
# checks the DP's against it at the end.
#
# ffn-dpsh is SINGLE-SESSION. Every call below is sequential for that reason;
# do not parallelise the loop.
set -eu

SRC=${1:-/tmp/ffn-dp-octeon}
DST=${2:-/tmp/dpb}
STAGE_MB=1

[ -f "$SRC" ] || { echo "no such file: $SRC"; exit 2; }

SIZE=$(stat -c %s "$SRC")
WANT=$(sha256sum "$SRC" | cut -d' ' -f1)
CHUNKS=$(( (SIZE + 1048575) / 1048576 ))

echo "source : $SRC"
echo "size   : $SIZE bytes ($CHUNKS chunks of ${STAGE_MB}M)"
echo "sha256 : $WANT"

i=0
while [ "$i" -lt "$CHUNKS" ]; do
	dd if="$SRC" of=/tmp/.dpchunk bs=1M skip="$i" count="$STAGE_MB" 2>/dev/null
	python3 /usr/local/bin/ffn_dpstage.py /tmp/.dpchunk >/dev/null 2>&1 \
		|| { echo "  chunk $i: stage FAILED"; exit 3; }

	# First chunk creates the file; the rest append. Getting this backwards
	# produces a file of the right length made entirely of the last chunk.
	if [ "$i" -eq 0 ]; then
		R="dd if=/dev/mem of=$DST bs=1M skip=5 count=$STAGE_MB 2>/dev/null"
	else
		R="dd if=/dev/mem bs=1M skip=5 count=$STAGE_MB 2>/dev/null >> $DST"
	fi
	/usr/local/bin/ffn-dpsh -c "$R" -t 120 >/dev/null 2>&1 \
		|| { echo "  chunk $i: DP append FAILED"; exit 4; }

	i=$((i + 1))
	echo "  chunk $((i - 1)) of $CHUNKS"
done
rm -f /tmp/.dpchunk

# The last chunk carries up to a megabyte of whatever followed the file in the
# staging window, so the length has to be restored before the hash will match.
# Strip ANSI colour before matching, and do NOT anchor to line start: the DP's
# shell colourises filenames, so the hash line can arrive as
# "<esc>[0;0m<hash>  <esc>[1;32m/tmp/dpb<esc>[m". An anchored match found
# nothing and reported "<no hash returned>" for a transfer that was perfectly
# fine -- the file was the right size and every chunk had verified.
GOT=$(/usr/local/bin/ffn-dpsh -c "truncate -s $SIZE $DST; chmod +x $DST; sha256sum $DST" -t 120 2>/dev/null \
      | tr -d '\r' | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' \
      | grep -oE '[0-9a-f]{64}' | head -1)

if [ "$GOT" = "$WANT" ]; then
	echo "verified: $DST on the DP matches"
else
	echo "MISMATCH on the DP"
	echo "  want $WANT"
	echo "  got  ${GOT:-<no hash returned>}"
	echo "If every chunk staged cleanly and only this differs, suspect a stale"
	echo "$SRC on THIS host -- see the note at the top of this script."
	exit 5
fi

cat <<EOF

The DP needs these before the binary can run:
  insmod ffn_octeon_info.ko          -> /proc/octeon_info
  insmod ffn_xkphys.ko               -> user XKPHYS access
  mount -t tmpfs tmpfs /dev/shm      -> the CVMX_SHARED region
Then:
  $DST --probe                        (what the helper thinks each iface is)
  $DST -p 0xa00 -s 3                  (run on the 40G, BGX2 index 0)
EOF
