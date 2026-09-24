# FFN OCTEON hardware drivers. Build against the exact prepared Linux 6.18+
# CP/DP kernel, using a current MIPS64 big-endian GNU toolchain.
# KDIR must contain .config and Module.symvers for the intended image.

obj-m += ffn_bcm.o
obj-m += ffn_bde.o
obj-m += ffn_mdio.o
obj-m += ffn_mdioctl.o
obj-m += ffn_fe100.o
obj-m += ffn_octeon_info.o
obj-m += ffn_xkphys.o

KDIR  ?=
CROSS ?= mips64-linux-gnuabi64-
KBUILD_ARGS = ARCH=mips CROSS_COMPILE=$(CROSS)

check-kernel:
	@test -n "$(KDIR)" && test -f "$(KDIR)/Makefile" || { echo 'Set KDIR to the prepared image kernel'; exit 1; }
	@v=$$($(MAKE) -s -C "$(KDIR)" kernelversion); major=$${v%%.*}; tail=$${v#*.}; minor=$${tail%%.*}; \
	  test "$$major" -gt 6 || { test "$$major" -eq 6 && test "$$minor" -ge 18; } || { echo "Legacy OCTEON kernel rejected: $$v"; exit 1; }

all: ffn_bcm.ko ffn_bde.ko ffn_bcmctl

ffn_bcm.ko: ffn_bcm.c ffn_bcm_abi.h | check-kernel
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) modules

# ffn_bde provides /dev/linux-user-bde so the vendor SDK shell can attach and
# initialise the switch in place. Same kbuild invocation; kbuild builds every
# obj-m in one pass, so this target exists for clarity rather than isolation.
ffn_bde.ko: ffn_bde.c ffn_bde_abi.h | check-kernel
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

# Validate hardware sources with the target kernel headers.
strict: check-kernel
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) KCFLAGS="-Werror" modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_ARGS) clean
	rm -f ffn_bcmctl

.PHONY: all strict clean check-kernel
