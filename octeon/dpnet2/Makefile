# ffn_dpnetd -- static big-endian MIPS64, runs on both OCTEONs (CP and DP).
#
# Built on the build server, which has the cross toolchain. The
# result is copied to the CP through the MP's NFS rootfs and to the DP over the
# ffn-dpsh channel.
#
# Static on purpose: the CP userland is the vendor's big-endian CentOS root plus
# FFN's static busybox, and the DP runs a Buildroot glibc. A static binary needs
# neither to match.

CROSS   ?= mips64-linux-gnuabi64-
CC      := $(CROSS)gcc
STRIP   := $(CROSS)strip

# -EB is explicit rather than relied upon. A silently little-endian build would
# read every control field byte-reversed and look like flaky hardware, so `make
# check` below re-verifies it on the finished binary.
CFLAGS  := -O2 -Wall -Wextra -std=gnu99 -EB -mabi=64
LDFLAGS := -static

BIN  := ffn_dpnetd
SRCS := ffn_dpnetd.c
HDRS := ffn_dpnet_ring.h

all: $(BIN) check

$(BIN): $(SRCS) $(HDRS)
	$(CC) $(CFLAGS) -o $@ $(SRCS) $(LDFLAGS)
	$(STRIP) $@

# Refuse to ship a binary that is not what both OCTEONs need. Each of these has
# a plausible failure mode: wrong endianness from a missing -EB, a dynamic link
# that will not resolve in the CP's userland, or a 32-bit ABI from the wrong
# multilib default.
check: $(BIN)
	@f=`file $(BIN)`; echo "$$f"; \
	echo "$$f" | grep -q "MSB"            || { echo "FAIL: not big-endian"; exit 1; }; \
	echo "$$f" | grep -q "statically linked" || { echo "FAIL: not static"; exit 1; }; \
	echo "$$f" | grep -q "64-bit"         || { echo "FAIL: not 64-bit"; exit 1; }; \
	echo "ok: big-endian, static, 64-bit MIPS"

clean:
	rm -f $(BIN)

.PHONY: all check clean
