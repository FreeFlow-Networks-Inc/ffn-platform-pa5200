# FFN OCTEON III kernel control driver for the BCM88375 CMIC.
#
# Cross-built against the same kernel that boots on the 5220's Octeon: Linux
# 4.9.57 from SDK-5.1.0, with the SDK's own mips64-octeon-linux-gnu-gcc 4.7.0.
# KDIR is the O= output directory of that build, not the source tree: it is the
# only one that has .config and Module.symvers, which modpost needs.
#
#   make                       # build ffn_bcm.ko AND ffn_bcmctl
#   make ffn_bcm.ko            # the module only
#   make ffn_bcmctl            # the userspace tool only
#   make clean
#   make KDIR=/path/to/kbuild  # override
#
# The result is a MIPS64 big-endian module. Check before shipping it anywhere:
#   file ffn_bcm.ko            -> ELF 64-bit MSB relocatable, MIPS
#   modinfo ffn_bcm.ko         -> vermagic 4.9.57 ... (must match the kernel)

obj-m += ffn_bcm.o

SDK   ?= /mnt/clones/sdk51/OCTEON-SDK
KDIR  ?= /mnt/clones/kbuild
CROSS ?= $(SDK)/tools/bin/mips64-octeon-linux-gnu-

# HOSTCFLAGS="-fcommon": this tree's scripts/dtc fails to link against a modern
# host GCC with "multiple definition of 'yylloc'". Same flag the kernel build
# needs; harmless here, required if kbuild rebuilds any host tool.
KBUILD_ARGS = ARCH=mips CROSS_COMPILE=$(CROSS) HOSTCFLAGS="-fcommon -O2"

all: ffn_bcm.ko ffn_bcmctl

ffn_bcm.ko: ffn_bcm.c ffn_bcm_abi.h
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules

# ffn_bcmctl is FREESTANDING: it has its own _start and makes syscalls through
# inline asm, so it links no libc. There MUST be an explicit rule for it --
# without one, make falls through to its builtin %: %.c rule, which uses the
# HOST compiler, and the MIPS register names in the syscall clobber lists come
# out as "unknown register name '$13' in 'asm'". That error looks like a bug in
# the source and is not; it just means the wrong compiler ran.
#
# The flag set matters and is the same one ffn_nfsmount needs:
#   -mno-abicalls -fno-pic -G0   no GOT, no small-data area: there is no dynamic
#                                loader and no runtime to set $gp up
#   -nostdlib -static            no libc, no startup files
#   -Wl,-e,_start                our entry point, since crt1.o is absent
#   -EB -mabi=64                 big-endian n64, matching the Octeon userland
BCMCTL_CFLAGS = -O2 -EB -mabi=64 -mno-abicalls -fno-pic -G0 -Wall
BCMCTL_LDFLAGS = -nostdlib -static -Wl,-e,_start

ffn_bcmctl: ffn_bcmctl.c ffn_bcm_abi.h
	$(CROSS)gcc $(BCMCTL_CFLAGS) $(BCMCTL_LDFLAGS) -o $@ $<
	@file $@ | grep -q 'MSB.*MIPS' || { 		echo "ffn_bcmctl: NOT a big-endian MIPS binary -- wrong compiler ran"; 		rm -f $@; exit 1; }

# Warnings-as-errors pass over THIS file only.
#
# Not -Wextra: on a 4.9 tree built by gcc 4.7 that lights up the kernel own
# headers (mmzone.h sign-compare, fs.h type-limits), so it reports nothing
# about this driver. kbuild already compiles with -Wall, so adding -Werror on
# top is the check that actually means something here.
strict:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) KCFLAGS="-Werror" modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) clean
	rm -f ffn_bcmctl

.PHONY: all strict clean
