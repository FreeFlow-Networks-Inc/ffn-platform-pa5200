# Build the OCTEON-side ffn_pcnetd: big-endian MIPS64 n64, for the CP initramfs.
#
# This used to build -static with a Debian mips64-linux-gnuabi64- cross gcc. Do
# not go back to that:
#
#   * that Debian cross toolchain is not present on any machine here, and
#   * building -static with the toolchain that IS here -- the OCTEON SDK's gcc
#     4.7 -- produces a binary that DIES on a modern kernel. Under upstream
#     6.18 it took a SIGSEGV inside ptmalloc_init (reached from
#     malloc_hook_ini), i.e. on its first malloc, before a line of its own code
#     ran. That is the SDK's 2012-era static glibc, not this daemon and not
#     /dev/mem: busybox from the same initramfs (glibc 2.41) reads 0x29000000
#     through /dev/mem without complaint.
#
# So: compile with the SDK toolchain, which is the only mips64 big-endian
# compiler here that has a libc at all (the kernel.org crosstool used for the
# kernel is nolibc and cannot link userland), but link DYNAMICALLY against the
# glibc that actually ships in the CP initramfs. Every other binary in that
# rootfs -- busybox, bash, python3.12 -- is dynamic against the same glibc and
# runs fine, so it is the proven target.
#
# The one snag is ffn_csu_compat.c: glibc 2.34 removed __libc_csu_init and
# __libc_csu_fini, and the SDK's crt1.o still references them. See that file.
#
# Result is ~9.6 KB instead of 3.6 MB, which is also a real win for an
# initramfs that is embedded in the kernel image.
#
# Verified 2026-09-02 on the PA-5220 CP under upstream 6.18.49: 200/200 pings
# at 1400 bytes, 0% loss, host counters tx=rx, zero drops.

# Toolchain and target rootfs. Both live on the RE VM by default; override on
# the command line if they move.
SDK        ?= /mnt/clones/sdk51/OCTEON-SDK/tools-gcc-4.7
CROSS      ?= $(SDK)/bin/mips64-octeon-linux-gnu-
TARGET_ROOT ?= /mnt/clones/fwdport/rootfs/tree

CC      = $(CROSS)gcc
STRIP   = $(CROSS)strip
CFLAGS  = -O2 -Wall -EB -mabi=64 -I.

# Link against the target rootfs glibc, and record the loader path as it will
# be at RUNTIME (/lib64/ld.so.1 inside the initramfs), not the build path.
# -nodefaultlibs keeps the SDK's own ancient libc out of the link; -lgcc is
# still needed for compiler builtins.
LDFLAGS = -Wl,--dynamic-linker=/lib64/ld.so.1 \
	  -Wl,-rpath-link,$(TARGET_ROOT)/lib64 \
	  -nodefaultlibs
LDLIBS  = $(TARGET_ROOT)/lib64/libc.so.6 -lgcc

TARGET  = ffn_pcnetd
SRCS    = ffn_pcnetd_oct.c ffn_csu_compat.c

$(TARGET): $(SRCS) ffn_pcnet.h
	@test -x "$(CC)" || { echo "no cross gcc at $(CC) -- set SDK= or CROSS="; exit 1; }
	@test -f "$(TARGET_ROOT)/lib64/libc.so.6" || { echo "no target glibc at $(TARGET_ROOT)/lib64/libc.so.6 -- set TARGET_ROOT="; exit 1; }
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRCS) $(LDLIBS)
	$(STRIP) $@
	@file $@
	@echo "reminder: this is DYNAMIC -- /lib64/ld.so.1 and libc.so.6 must be in the initramfs"

clean:
	rm -f $(TARGET)

.PHONY: clean
