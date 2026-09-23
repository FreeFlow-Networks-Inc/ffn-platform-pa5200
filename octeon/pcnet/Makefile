# Debian MIPS64 big-endian transport. Keep the executable resident in RAM
# before serving a network-backed root. Use a current GNU/glibc toolchain.
CROSS ?= mips64-linux-gnuabi64-
CC = $(CROSS)gcc
STRIP = $(CROSS)strip
CFLAGS = -O2 -Wall -Wextra -EB -mabi=64 -I.
LDFLAGS = -static
TARGET = ffn_pcnetd
SRCS = ffn_pcnetd_oct.c

$(TARGET): $(SRCS) ffn_pcnet.h
	@case "$$($(CC) -dumpmachine)" in *musl*|*openwrt*|mips64el*) exit 1;; mips64*-linux-gnu*) ;; *) echo 'Debian MIPS64 BE GNU compiler required'; exit 1;; esac
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRCS)
	$(STRIP) $@
	@file $@ | grep -E 'ELF 64-bit MSB.*MIPS.*statically linked'

clean:
	rm -f $(TARGET)

.PHONY: clean
