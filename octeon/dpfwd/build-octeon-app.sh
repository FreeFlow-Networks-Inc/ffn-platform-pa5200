#!/bin/sh
# Build the dataplane forwarder as an OCTEON III userspace application.
#
# Run on the RE VM. Produces a static mips64 big-endian binary that runs on the
# DP's own Linux, driving PKI/SSO/PKO3 directly.
#
# WHY THIS IS NOT `make oct3-cvmx`. That target compiles the CVMX backends to
# hold them to the real API and stops there -- it never links. This links, and
# linking is where every interesting problem was.
#
# WHY musl AND NOT THE SDK'S OWN COMPILER. The DP's initramfs is static busybox
# with no shared libraries, so the binary must be static; and the SDK's bundled
# 2012 glibc dies in ptmalloc_init on a modern kernel before main() runs. musl
# static with gcc 13.3 is measured working on 6.18. The cost is three
# compatibility headers in compat/ and one patched SDK line, all below.
#
# WHY THE SDK TREE IS COPIED. cvmx-access-native.h needs a one-line change (see
# PATCH below) and the SDK's headers include each other with quoted includes,
# which resolve next to the including file -- so -I cannot override them. The
# copy is also why this never writes to the SDK: vendor source is used in place
# and read-only, exactly like the vendor BCM config.
#
# TWO KERNEL MODULES MUST BE LOADED ON THE DP or the result cannot run:
#   octeon/kctl/ffn_octeon_info.c   -- /proc/octeon_info, else exit(-1) in init
#   octeon/kctl/ffn_xkphys.c        -- user XKPHYS, else SIGSEGV on first CSR
# and /dev/shm must be mounted (tmpfs) for the CVMX_SHARED region.
set -eu

SDK=${SDK:-/mnt/clones/sdk51/OCTEON-SDK}
TC=${TC:-/mnt/clones/openwrt-toolchain/toolchain-mips64_octeonplus_64_gcc-13.3.0_musl}
STAGE=${STAGE:-/tmp/sdkbuild}
OUT=${OUT:-ffn-dp-octeon}
MODEL=${MODEL:-OCTEON_CN78XX}

export STAGING_DIR="$TC"
CC="$TC/bin/mips64-openwrt-linux-musl-gcc"
AR="$TC/bin/mips64-openwrt-linux-musl-ar"
NM="$TC/bin/mips64-openwrt-linux-musl-nm"

[ -x "$CC" ] || { echo "no compiler at $CC"; exit 2; }
[ -d "$SDK/executive" ] || { echo "no SDK at $SDK"; exit 2; }

# ---------------------------------------------------------------------------
# 1. Stage the SDK sources and apply the one patch.
# ---------------------------------------------------------------------------
# PATCH: cvmx-access-native.h declares, INSIDE a function body,
#     extern CVMX_SHARED uint32_t cvmx_app_init_processor_id;
# and CVMX_SHARED becomes a section attribute here (see compat/). GCC rejects a
# section attribute on a function-local declaration -- "section attribute cannot
# be specified for local variables" -- while Cavium's own attribute allowed it.
# The attribute is meaningless on an extern declaration anyway; the definition
# elsewhere carries it. This is the ONLY such site in the whole executive,
# measured, not assumed.
#
# -L on the copy is required: target/include/* are symlinks into executive/, so
# a plain cp -a produces a tree of dangling links that `ls` shows and `cat`
# cannot read.
if [ ! -d "$STAGE" ] || [ "${REFRESH:-0}" = 1 ]; then
	echo "  staging SDK sources into $STAGE"
	rm -rf "$STAGE"
	mkdir -p "$STAGE"
	cp -a  "$SDK/executive"     "$STAGE/executive"
	cp -aL "$SDK/target/include" "$STAGE/include"
	for f in "$STAGE/executive/cvmx-access-native.h" \
	         "$STAGE/include/cvmx-access-native.h"; do
		sed -i 's/^\textern CVMX_SHARED uint32_t cvmx_app_init_processor_id;/\textern uint32_t cvmx_app_init_processor_id;   \/* FFN: section attr is invalid on a function-local extern *\//' "$f"
	done
	grep -q 'FFN: section attr' "$STAGE/include/cvmx-access-native.h" \
		|| { echo "  PATCH DID NOT APPLY -- SDK layout changed?"; exit 3; }
fi

CF="-march=octeon3 -mabi=64 -EB -O2 -std=gnu99 -w
    -include compat/ffn_musl_compat.h
    -DCVMX_BUILD_FOR_LINUX_USER=1
    -DUSE_RUNTIME_MODEL_CHECKS=1
    -DCVMX_ENABLE_POW_CHECKS=0
    -DCVMX_ENABLE_PARAMETER_CHECKING=0
    -DCVMX_ENABLE_CSR_ADDRESS_CHECKING=0
    -DOCTEON_MODEL=$MODEL
    -Icompat -I$STAGE/executive/libfdt -I$STAGE/include -I."

# ---------------------------------------------------------------------------
# 2. The executive -> libcvmx.a
# ---------------------------------------------------------------------------
# An ARCHIVE, not a pile of objects: the linker then pulls only what the call
# graph reaches. Five executive files do not build against musl at all
# (cvmx-interrupt aside, they need glibc-only headers), and none of them is
# reachable, so an archive makes them a non-problem where `ld -r` made them
# undefined symbols.
#
# cvmx-app-init.c is EXCLUDED deliberately: it is the bare-metal twin of
# cvmx-app-init-linux.c and defines the same cvmx_user_app_init.
echo "  building executive"
rm -rf cvmx-obj-musl && mkdir -p cvmx-obj-musl
n=0
for f in "$STAGE"/executive/cvmx-*.c "$STAGE"/executive/octeon-*.c; do
	b=$(basename "$f" .c)
	[ "$b" = "cvmx-app-init" ] && continue
	# shellcheck disable=SC2086
	$CC -c $CF -o "cvmx-obj-musl/$b.o" "$f" 2>/dev/null && n=$((n + 1)) || true
done
for f in "$STAGE"/executive/libfdt/*.c; do
	b=$(basename "$f" .c)
	# shellcheck disable=SC2086
	$CC -c $CF -o "cvmx-obj-musl/fdt_$b.o" "$f" 2>/dev/null && n=$((n + 1)) || true
done
echo "  executive: $n objects"
[ "$n" -gt 130 ] || { echo "  too few -- expected ~143"; exit 4; }

rm -f libcvmx.a
$AR rcs libcvmx.a cvmx-obj-musl/*.o

# ---------------------------------------------------------------------------
# 3. FFN's own sources
# ---------------------------------------------------------------------------
echo "  building forwarder"
rm -rf musl-obj && mkdir -p musl-obj
for f in ffn_dp_oct.c ffn_dp_l3.c ffn_dp_l3_config.c ffn_dp_arp.c \
         ffn_dp_engine.c ffn_dp_dlp.c ffn_dp_vsys.c \
         ffn_dp_io_octeon.c ffn_dp_io_octeon3.c ffn_dp_bgx_octeon3.c \
         ffn_dp_octeon_main.c; do
	# shellcheck disable=SC2086
	$CC -c $CF -DFFN_HAVE_CVMX=1 -o "musl-obj/$(basename "$f" .c).o" "$f"
done

# ---------------------------------------------------------------------------
# 4. Link
# ---------------------------------------------------------------------------
# The SDK's linker script is REQUIRED, not optional: it defines
# __cvmx_shared_start/__cvmx_shared_end and gathers .cvmx_shared between them.
# Without it the link fails on those two symbols.
echo "  linking"
$CC -march=octeon3 -mabi=64 -EB -static -o "$OUT" \
    musl-obj/*.o libcvmx.a -lm \
    -Wl,-T,"$SDK/target/lib/cvmx-shared-linux.ld"

# ---------------------------------------------------------------------------
# 5. Check the shared section is REAL
# ---------------------------------------------------------------------------
# This is the check that matters most, because its failure is completely silent.
# If CVMX_SHARED does not place variables, `pending_fork` is per-process,
# cvmx_user_app_init()'s barrier never completes, and all 40 cores spin at 100%
# forever with no output and no error. Equal start/end == that outcome.
s=$($NM "$OUT" | awk '/__cvmx_shared_start/{print $1}')
e=$($NM "$OUT" | awk '/__cvmx_shared_end/{print $1}')
echo "  .cvmx_shared: 0x$s - 0x$e"
if [ "$s" = "$e" ]; then
	echo "  FATAL: .cvmx_shared is EMPTY. CVMX_SHARED placed nothing, so the"
	echo "         per-core fork barrier will hang. Check that"
	echo "         compat/ffn_musl_compat.h defines cvmx_shared."
	exit 5
fi

ls -l "$OUT"
echo "  ok -- deploy with octeon/dpfwd/deploy-octeon-app.sh"
