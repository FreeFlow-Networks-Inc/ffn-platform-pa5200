# FFN OCTEON dataplane (OCTEON-II IPD/POW/PKO and OCTEON-III PKI/SSO/PKO3)
#
#   make            native build + test (x86-64, proves the logic)
#   make test       run the test harness
#   make mips64     cross-build for OCTEON (big-endian MIPS64)
#   make oct3-cvmx  compile the CVMX backends against a real OCTEON SDK
#   make qemu-test  run the SAME test under qemu-mips64 (validates big-endian)
#
# The forwarder has no chip headers, so the native and MIPS builds compile the
# identical sources; only the packet-I/O backend differs on real hardware.

CC      ?= cc
CFLAGS  ?= -O2 -g -Wall -Wextra -Wno-unused-parameter -std=gnu11
SRC      = ffn_dp_oct.c
TEST_SRC = ffn_dp_oct_test.c
# ffn_dp_oct.c includes ffn_dp_arp.h and ffn_dp_engine.h directly and pulls
# ffn_dp_l3.h in through ffn_dp_oct.h. None of them were listed here, so
# editing any of them did not rebuild ffn_dp_oct.o -- and a stale object still
# holding the old struct layout links and runs without complaint.
HDRS     = ffn_dp_abi.h ffn_dp_oct.h ffn_dp_bgx.h ffn_dp_l3.h ffn_dp_arp.h \
           ffn_dp_engine.h ffn_dp_dlp.h

# --- L3 / neighbour ---
# Not optional extras: ffn_dp_oct.c calls dp_l3_lookup(), dp_arp_input(),
# dp_arp_request() and dp_arp_tick() directly, so anything that links
# ffn_dp_oct.o needs these too. They were absent from every target, which meant
# the integrated dataplane -- forwarder plus routing -- had never once linked;
# only the standalone L3 and ARP tests, built by hand, ever compiled this code.
L3_SRC   = ffn_dp_l3.c ffn_dp_l3_config.c ffn_dp_arp.c
L3_HDRS  = ffn_dp_l3.h ffn_dp_l3_config.h ffn_dp_arp.h
L3_OBJ   = $(L3_SRC:.c=.o)

# --- inline analysis engines ---
# The control plane steers where L3 goes; inline analysis lives here. Kept
# separate from the forwarder so the engine tests link without dragging in the
# packet-I/O backends -- the scanners are pure functions over a byte range and
# --- virtual systems ---
# The plan half of hardware-enforced vsys: which SSO group, QPG entry and PKI
# style each tenant gets. Portable and chip-free on purpose -- it is the part
# that is easy to get wrong (PCAM ADDS to a style, it does not set one) and
# the only part testable without an OCTEON.
VSYS_SRC  = ffn_dp_vsys.c
VSYS_HDRS = ffn_dp_vsys.h
VSYS_OBJ  = $(VSYS_SRC:.c=.o)

$(VSYS_OBJ): %.o: %.c $(VSYS_HDRS) ffn_dp_oct.h
	$(CC) $(CFLAGS) -c $< -o $@

# should be testable as such.
ENG_SRC  = ffn_dp_engine.c ffn_dp_dlp.c
ENG_HDRS = ffn_dp_engine.h ffn_dp_dlp.h
ENG_OBJ  = $(ENG_SRC:.c=.o)

# ENG_OBJ was defined and then referenced by no rule at all: the engine test
# compiled $(ENG_SRC) directly, so the objects were never built. Now that the
# forwarder calls dp_engine_scan(), every link line below needs them.
$(ENG_OBJ): %.o: %.c $(ENG_HDRS) ffn_dp_oct.h
	$(CC) $(CFLAGS) -c $< -o $@

# OCTEON-II: MIPS64 release 2, BIG-endian. Upstream toolchain preferred over the
# Cavium SDK (licence hygiene -- FFN ships nothing SDK-derived).
MIPS_CC     ?= mips64-linux-gnuabi64-gcc
MIPS_CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter -std=gnu11 \
               -march=mips64r2 -mabi=64 -EB -static

AFP_SRC  = ffn_dp_io_afpacket.c
AFP_MAIN = ffn_dp_afpacket_main.c
AFP_HDRS = $(HDRS) ffn_dp_io_afpacket.h

all: ffn_dp_oct_test ffn_dp_afpacket dp_serve

ffn_dp_oct.o: $(SRC) $(HDRS)
	$(CC) $(CFLAGS) -c $(SRC) -o $@

# Explicit rather than leaning on make's built-in %.o rule, so that editing an
# L3 header actually rebuilds these objects.
$(L3_OBJ): %.o: %.c $(HDRS) $(L3_HDRS)
	$(CC) $(CFLAGS) -c $< -o $@

ffn_dp_oct_test: ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(VSYS_OBJ) $(TEST_SRC) $(HDRS)
	$(CC) $(CFLAGS) ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(VSYS_OBJ) $(TEST_SRC) -o $@

test: ffn_dp_oct_test
	./ffn_dp_oct_test

# runnable dataplane over real Linux interfaces (Linux-only backend)
ffn_dp_afpacket: ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(AFP_SRC) $(AFP_MAIN) $(AFP_HDRS)
	$(CC) $(CFLAGS) ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(AFP_SRC) $(AFP_MAIN) -o $@

# end-to-end forwarding test over veth pairs (needs root)
veth-test: ffn_dp_afpacket
	./veth_test.sh

# --- OCTEON-II hardware backend (IPD/PIP + POW + PKO) ---
# Built WITHOUT the SDK by default: the hardware seam compiles and reports the
# backend unavailable, and the mock-hardware test still verifies the FPA
# buffer-ownership contract. To build the real path, supply an OCTEON SDK:
#   make oct-test CVMX=1 CVMX_INC=-I/path/to/octeon-sdk/target/include
OCT_SRC  = ffn_dp_io_octeon.c
OCT_TEST = ffn_dp_io_octeon_test.c
OCT_HDRS = $(HDRS) ffn_dp_io_octeon.h
ifdef CVMX
OCT_CFLAGS = -DFFN_HAVE_CVMX $(CVMX_INC)
else
OCT_CFLAGS =
endif

ffn_dp_oct_io_test: ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(OCT_SRC) $(OCT_TEST) $(OCT_HDRS)
	$(CC) $(CFLAGS) $(OCT_CFLAGS) ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(OCT_SRC) $(OCT_TEST) -o $@

oct-test: ffn_dp_oct_io_test
	./ffn_dp_oct_io_test

# --- OCTEON-III backend (PKI + SSO + PKO3) ---
# A PA-5220 carries two OCTEON-III parts: a CN73XX control plane and the 40-core
# CN78XX that runs this dataplane. The backend lives alongside the OCTEON-II one
# (a PA-3200 needs that) and is selected at runtime by oct_detect_gen().
OCT3_SRC  = ffn_dp_io_octeon3.c
OCT3_TEST = ffn_dp_io_octeon3_test.c
OCT3_HDRS = $(HDRS) ffn_dp_io_octeon.h ffn_dp_io_octeon3.h ffn_dp_bgx.h

ffn_dp_oct3_io_test: ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(OCT_SRC) $(OCT3_SRC) $(OCT3_TEST) $(OCT3_HDRS)
	$(CC) $(CFLAGS) $(OCT_CFLAGS) ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) $(OCT_SRC) $(OCT3_SRC) \
	    $(OCT3_TEST) -o $@

oct3-test: ffn_dp_oct3_io_test
	./ffn_dp_oct3_io_test

# The aura/descriptor logic is endianness-sensitive, and the real target is
# big-endian MIPS64, so the same suite runs there too.
oct3-qemu: $(SRC) $(OCT_SRC) $(OCT3_SRC) $(OCT3_TEST) $(OCT3_HDRS)
	@command -v $(MIPS_CC) >/dev/null 2>&1 || { echo "MISSING $(MIPS_CC)"; exit 1; }
	$(MIPS_CC) $(MIPS_CFLAGS) $(OCT_CFLAGS) $(SRC) $(L3_SRC) $(ENG_SRC) $(OCT_SRC) $(OCT3_SRC) \
	    $(OCT3_TEST) -o ffn_dp_oct3_io_test.mips64
	qemu-mips64 ./ffn_dp_oct3_io_test.mips64

# same backend test, big-endian, as it will run on the real Octeon
oct-qemu: $(SRC) $(OCT_SRC) $(OCT_TEST) $(OCT_HDRS)
	@command -v $(MIPS_CC) >/dev/null 2>&1 || { echo "MISSING $(MIPS_CC)"; exit 1; }
	$(MIPS_CC) $(MIPS_CFLAGS) $(OCT_CFLAGS) $(SRC) $(L3_SRC) $(ENG_SRC) $(OCT_SRC) $(OCT_TEST) \
	    -o ffn_dp_oct_io_test.mips64
	qemu-mips64 ./ffn_dp_oct_io_test.mips64

# --- compile-check the hardware backends against a real OCTEON SDK ---
# The CVMX call sites cannot be exercised without hardware, but they CAN be held
# to the real API. Every mistake this target has caught -- a made-up
# sub-descriptor code, an invented accessor, a call with the wrong arity, a hook
# that was never defined -- would otherwise have surfaced as a dataplane that
# builds, runs, and moves no packets.
#
#   make oct3-cvmx SDK=/path/to/OCTEON-SDK [MODEL=OCTEON_CN78XX]
#
# It compiles only; linking needs the SDK's libcvmx built for the same model.
SDK   ?=
MODEL ?= OCTEON_CN78XX
SDK_CC      = $(SDK)/tools/bin/mips64-octeon-linux-gnu-gcc
SDK_CFLAGS  = -c -march=octeon3 -mabi=64 -EB -O2 -g -std=gnu99 -Wall -Wextra \
              -Wno-unused-parameter -DFFN_HAVE_CVMX=1 \
              -DUSE_RUNTIME_MODEL_CHECKS=1 -DCVMX_ENABLE_POW_CHECKS=0 \
              -DCVMX_ENABLE_PARAMETER_CHECKING=0 \
              -DCVMX_ENABLE_CSR_ADDRESS_CHECKING=0 \
              -DOCTEON_MODEL=$(MODEL) -I$(SDK)/target/include -I.
CVMX_SRCS   = $(SRC) $(L3_SRC) $(ENG_SRC) $(VSYS_SRC) $(OCT_SRC) $(OCT3_SRC) ffn_dp_bgx_octeon3.c

oct3-cvmx:
	@test -n "$(SDK)" || { echo "set SDK=/path/to/OCTEON-SDK"; exit 1; }
	@test -x $(SDK_CC) || { echo "MISSING $(SDK_CC)"; exit 1; }
	@mkdir -p cvmx-obj
	@for f in $(CVMX_SRCS); do \
	    echo "  CVMX-CC $$f"; \
	    $(SDK_CC) $(SDK_CFLAGS) -o cvmx-obj/`basename $$f .c`.o $$f || exit 1; \
	 done
	@echo "octeon backends compile clean against $(MODEL)"

# everything that can run without Octeon hardware
check: test l3-test arp-test engine-test vsys-test oct-test oct3-test
	@echo "--- big-endian ---"
	@$(MAKE) -s qemu-test
	@$(MAKE) -s oct-qemu
	@$(MAKE) -s oct3-qemu
	@$(MAKE) -s l3-qemu
	@$(MAKE) -s arp-qemu
	@$(MAKE) -s engine-qemu
	@$(MAKE) -s vsys-qemu

# --- cross build for the real target ---
mips64: $(SRC) $(L3_SRC) $(ENG_SRC) $(TEST_SRC) $(HDRS) $(L3_HDRS)
	@command -v $(MIPS_CC) >/dev/null 2>&1 || { \
	  echo "MISSING $(MIPS_CC)"; \
	  echo "  Debian/Ubuntu: apt install gcc-mips64-linux-gnuabi64"; \
	  echo "  (or a Buildroot OCTEON-II toolchain)"; exit 1; }
	$(MIPS_CC) $(MIPS_CFLAGS) $(SRC) $(L3_SRC) $(ENG_SRC) $(TEST_SRC) -o ffn_dp_oct_test.mips64
	@file ffn_dp_oct_test.mips64

# Same expectations, big-endian: catches any accidental native-endian access.
qemu-test: mips64
	@command -v qemu-mips64 >/dev/null 2>&1 || { \
	  echo "MISSING qemu-mips64 (apt install qemu-user)"; exit 1; }
	qemu-mips64 ./ffn_dp_oct_test.mips64

clean:
	$(RM) ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) \
	      ffn_dp_oct_test ffn_dp_oct_test.mips64 ffn_dp_afpacket dp_serve \
	      ffn_dp_oct_io_test ffn_dp_oct_io_test.mips64 \
	      ffn_dp_oct3_io_test ffn_dp_oct3_io_test.mips64 \
	      ffn_dp_l3_test ffn_dp_l3_test.mips64 \
	      ffn_dp_arp_test ffn_dp_arp_test.mips64 \
	      ffn_dp_engine_test ffn_dp_engine_test.mips \
      ffn_dp_vsys_test ffn_dp_vsys_test.mips
	
	$(RM) -r cvmx-obj

.PHONY: all test l3-test arp-test engine-test engine-qemu vsys-test vsys-qemu mips64 qemu-test veth-test oct-test oct-qemu oct3-test \
        oct3-qemu oct3-cvmx l3-qemu arp-qemu interop check clean

# interop harness: runs the real DP command loop over an mmap'd file so the
# Python MP client is tested against actual C code, not a second model of the
# wire format. Depends on the .o so a stale binary cannot silently pass.
dp_serve: ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) dp_serve.c $(HDRS)
	$(CC) $(CFLAGS) ffn_dp_oct.o $(L3_OBJ) $(ENG_OBJ) dp_serve.c -o $@

interop: dp_serve
	python3 interop_test.py

# --- L3 / ARP unit suites ---
# These had no target at all. Both test files existed and both were run by hand
# with ad-hoc cc lines, which means a regression in either would have been
# invisible to `make check` -- the one command anyone actually runs.
ffn_dp_l3_test: $(L3_OBJ) ffn_dp_l3_test.c $(L3_HDRS)
	$(CC) $(CFLAGS) $(L3_OBJ) ffn_dp_l3_test.c -o $@

# Engines. Built and run BOTH natively and big-endian: the scanners walk packet
# bytes, and "passes on x86" is not evidence for an OCTEON. engine-qemu runs the
# identical binary under qemu-mips64; that same binary also runs unmodified on
# the real CP, which is how it was first validated.
ffn_dp_engine_test: $(ENG_SRC) ffn_dp_engine_test.c $(ENG_HDRS) ffn_dp_oct.h
	$(CC) $(CFLAGS) -o $@ $(ENG_SRC) ffn_dp_engine_test.c

engine-test: ffn_dp_engine_test
	./ffn_dp_engine_test

ffn_dp_vsys_test: $(VSYS_SRC) ffn_dp_vsys_test.c $(VSYS_HDRS) ffn_dp_oct.h
	$(CC) $(CFLAGS) -o $@ $(VSYS_SRC) ffn_dp_vsys_test.c

vsys-test: ffn_dp_vsys_test
	./ffn_dp_vsys_test

ffn_dp_vsys_test.mips: $(VSYS_SRC) ffn_dp_vsys_test.c $(VSYS_HDRS) ffn_dp_oct.h
	$(MIPS_CC) $(MIPS_CFLAGS) -o $@ $(VSYS_SRC) ffn_dp_vsys_test.c

vsys-qemu: ffn_dp_vsys_test.mips
	qemu-mips64 ./ffn_dp_vsys_test.mips

ffn_dp_engine_test.mips: $(ENG_SRC) ffn_dp_engine_test.c $(ENG_HDRS) ffn_dp_oct.h
	$(MIPS_CC) $(MIPS_CFLAGS) -o $@ $(ENG_SRC) ffn_dp_engine_test.c

engine-qemu: ffn_dp_engine_test.mips
	qemu-mips64 ./ffn_dp_engine_test.mips

l3-test: ffn_dp_l3_test
	./ffn_dp_l3_test

ffn_dp_arp_test: $(L3_OBJ) ffn_dp_arp_test.c $(L3_HDRS)
	$(CC) $(CFLAGS) $(L3_OBJ) ffn_dp_arp_test.c -o $@

arp-test: ffn_dp_arp_test
	./ffn_dp_arp_test

# The L3 and ARP code is the most endianness-sensitive in the tree -- IPv4
# addresses, the RFC 1624 incremental checksum update, MAC and ARP field
# layout -- and the real target is big-endian, so running these suites ONLY on
# x86 tests them on the one byte order they will never see in production.
l3-qemu: $(L3_SRC) ffn_dp_l3_test.c $(L3_HDRS)
	@command -v $(MIPS_CC) >/dev/null 2>&1 || { echo "MISSING $(MIPS_CC)"; exit 1; }
	$(MIPS_CC) $(MIPS_CFLAGS) $(L3_SRC) ffn_dp_l3_test.c -o ffn_dp_l3_test.mips64
	qemu-mips64 ./ffn_dp_l3_test.mips64

arp-qemu: $(L3_SRC) ffn_dp_arp_test.c $(L3_HDRS)
	@command -v $(MIPS_CC) >/dev/null 2>&1 || { echo "MISSING $(MIPS_CC)"; exit 1; }
	$(MIPS_CC) $(MIPS_CFLAGS) $(L3_SRC) ffn_dp_arp_test.c -o ffn_dp_arp_test.mips64
	qemu-mips64 ./ffn_dp_arp_test.mips64
