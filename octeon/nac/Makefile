# FFN NAC (Nir-Aho-Corasick) accelerator driver for the PA-5200 CP.
#
# TARGET
#   The CP OCTEON III (CN73XX) running our Debian-rooted upstream kernel
#   6.18.49-ffn-debian-cp. These devices live on the CP's own
#   PCIe domain 0003 (CA1 was seen at 0003:06:00.0), so this module only has
#   anything to bind to on the CP -- it is NOT an x86/MP module.
#
#   Primary target is the CE40 (feed:a00d): it is the family member PAN-OS
#   11.2 still ships a driver for, and the only one with RLD calibration.
#   CA1 (feed:a101) stays in the id table because it is the one measured
#   present on our board.
#
# WHERE TO BUILD
#   On the RE VM (stephen@192.168.47.129), NOT on the MP and NOT on the CP:
#     * the CP has no toolchain at all -- gcc, cc, make and ld are all absent
#       from the 91-package Debian root, and there are no kernel headers;
#     * the MP has no mips64 cross compiler;
#     * the CP's own /lib/modules/$(uname -r)/build symlink already points at
#       /mnt/clones/fwdport/debian-candidates/linux-cp, which is a path on the
#       RE VM. That is the tree the running kernel was built from, so it is the
#       only one with a matching .config and Module.symvers for modpost.
#
#   Build inside the mips64 cross chroot:
#     chroot /mnt/clones/debian-mips64/sid-host
#     cd .../octeon/nac && make
#
# CHECK BEFORE SHIPPING
#   file ffn_nac.ko      -> ELF 64-bit MSB relocatable, MIPS
#   modinfo ffn_nac.ko   -> vermagic must match the running CP kernel exactly,
#                           alias must list pci:v0000FEEDd0000A00D (CE40)
#                           and pci:v0000FEEDd0000A101 (CA1)
#
# LOADING
#   Read-only by default. Writes to the device window are refused unless:
#     insmod ffn_nac.ko allow_write=1
#   Bus mastering is never enabled by this driver, so the device cannot DMA
#   into host memory regardless.

obj-m += ffn_nac.o

KDIR          ?= /mnt/clones/fwdport/debian-candidates/linux-cp
ARCH          ?= mips
CROSS_COMPILE ?= mips64-linux-gnuabi64-

PWD := $(shell pwd)

.PHONY: all clean modinfo install-cp

all:
	$(MAKE) -C $(KDIR) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS_COMPILE) \
		M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS_COMPILE) \
		M=$(PWD) clean

# Sanity-check the artifact before it goes anywhere near the hardware.
modinfo: all
	@file ffn_nac.ko
	@modinfo ffn_nac.ko | egrep '^(filename|description|license|alias|vermagic)'

# Stage into the CP's NFS root on the MP. The CP roots on /opt/ffn-cproot,
# which is an ordinary directory on the MP's SSD, so this is a plain copy.
CPROOT ?= /opt/ffn-cproot
install-cp: all
	install -D -m 0644 ffn_nac.ko \
		$(CPROOT)/lib/modules/extra/ffn_nac.ko
	@echo "staged -> $(CPROOT)/lib/modules/extra/ffn_nac.ko"
