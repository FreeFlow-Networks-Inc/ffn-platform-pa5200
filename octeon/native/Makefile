CC ?= cc
CFLAGS ?= -O2 -g -std=gnu11 -Wall -Wextra -Werror -pthread

all: libffn-packet.so libffn-hwio.so libffn-inline.so

libffn-packet.so: ffn_packet.c ffn_packet.h ffn_aggregate_packet.inc
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now ffn_packet.c -o $@

libffn-hwio.so: ffn_hwio.c
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now ffn_hwio.c -o $@

INLINE = ../dpfwd/ffn_inline_adapter.c ../dpfwd/ffn_dp_engine.c ../dpfwd/ffn_dp_dlp.c
libffn-inline.so: $(INLINE) $(wildcard ../dpfwd/*.h)
	$(CC) $(CFLAGS) -Wno-unused-parameter -fPIC -shared -Wl,-z,relro,-z,now $(INLINE) -o $@

test-scan.so: test_scan.c
	$(CC) $(CFLAGS) -fPIC -shared test_scan.c -o $@

test-hwio: ffn_hwio.c test_hwio.c
	$(CC) $(CFLAGS) ffn_hwio.c test_hwio.c -Wl,--wrap=open,--wrap=close,--wrap=ioctl -o $@

test: libffn-packet.so libffn-inline.so test-scan.so test-hwio
	./test-hwio
	FFN_PACKET_LIB=$(CURDIR)/libffn-packet.so python3 test_packet.py
	python3 test_aggregate.py

.PHONY: all test
