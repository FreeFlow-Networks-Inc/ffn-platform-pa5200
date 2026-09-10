#!/bin/sh
# VM: compile against the running virtual DP kernel, SDK remains read-only.
set -eu
SDK=${SDK:-/mnt/clones/sdk51/OCTEON-SDK}
KDIR=${KDIR:-/mnt/clones/fwdport/debian-candidates/linux-dp-virtual}
OUT=${OUT:-/home/stephen/ffn-sdk-aes}
SOURCE=${1:?usage: build-sdk-aes.sh PATH_TO_ffn_octeon_aes.c}
CROSS=/mnt/clones/fwdport/gcc-14.4.0-nolibc/mips64-linux/bin/mips64-linux-
mkdir -p "$OUT"
cp "$SOURCE" "$OUT/ffn_octeon_aes.c"
python3 - "$SDK/executive/cvmx-asm.h" "$OUT/ffn_sdk_crypto_ops.h" <<'PY'
import pathlib,re,sys
names={'CVMX_MT_AES_KEY','CVMX_MT_AES_KEYLENGTH','CVMX_MT_AES_ENC0','CVMX_MT_AES_ENC1',
       'CVMX_MT_AES_DEC0','CVMX_MT_AES_DEC1','CVMX_MF_AES_RESULT'}
lines=[]
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    m=re.match(r'#define\s+(\w+)\(',line)
    if m and m.group(1) in names:
        names.remove(m.group(1));lines.append(line)
if names: raise SystemExit('SDK missing macros: '+str(names))
pathlib.Path(sys.argv[2]).write_text('#define CVMX_TMP_STR2(x) #x\n#define CVMX_TMP_STR(x) CVMX_TMP_STR2(x)\n'+'\n'.join(lines)+'\n')
PY
printf 'obj-m += ffn_octeon_aes.o\n' > "$OUT/Makefile"
make -C "$KDIR" M="$OUT" ARCH=mips CROSS_COMPILE="$CROSS" KCFLAGS=-Werror modules
sha256sum "$OUT/ffn_octeon_aes.ko"
