# FFN OCTEON III kernel control driver for the BCM88375 CMIC.
#
# Cross-built against the same kernel that boots on the 5220's Octeon: Linux
# 4.9.57 from SDK-5.1.0, with the SDK's own mips64-octeon-linux-gnu-gcc 4.7.0.
# KDIR is the O= output directory of that build, not the source tree: it is the
# only one that has .config and Module.symvers, which modpost needs.
#
#   make                       # build ffn_bcm.ko
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

all: ffn_bcm.ko

ffn_bcm.ko: ffn_bcm.c ffn_bcm_abi.h
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules

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

.PHONY: all strict clean
