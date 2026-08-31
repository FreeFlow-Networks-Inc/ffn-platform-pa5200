# Build the OCTEON-side ffn_pcnetd, static big-endian MIPS64 so it drops into the
# lean initramfs with no libc dependency. Same class of binary as ffn_cpdpd.
CROSS  ?= mips64-linux-gnuabi64-
CC      = $(CROSS)gcc
CFLAGS  = -O2 -Wall -static -EB -mabi=64 -I.
TARGET  = ffn_pcnetd

$(TARGET): ffn_pcnetd_oct.c ffn_pcnet.h
	$(CC) $(CFLAGS) -o $@ ffn_pcnetd_oct.c
	@file $@

clean:
	rm -f $(TARGET)
