CC ?= cc
CFLAGS ?= -O2 -g -std=gnu11 -Wall -Wextra -Werror -pthread

all: libffn-packet.so libffn-hwio.so libffn-inline.so

libffn-packet.so: ffn_packet.c ffn_packet.h ffn_aggregate_packet.inc ffn_fe100_packet.inc ffn_fe100_punt.c ffn_fe100_punt.h ffn_fe100_stats.c ffn_fe100_stats.h
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now ffn_packet.c ffn_fe100_punt.c ffn_fe100_stats.c -o $@

test-fe100-punt: ffn_fe100_punt.c ffn_fe100_punt.h test_fe100_punt.c
	$(CC) $(CFLAGS) ffn_fe100_punt.c test_fe100_punt.c -o $@

test-fe100-stats: ffn_fe100_stats.c ffn_fe100_stats.h test_fe100_stats.c
	$(CC) $(CFLAGS) ffn_fe100_stats.c test_fe100_stats.c -o $@

ffn-fe100-stats-probe: ffn_fe100_stats.c ffn_fe100_stats.h ffn_fe100_stats_probe.c
	$(CC) $(CFLAGS) ffn_fe100_stats.c ffn_fe100_stats_probe.c -o $@

test-fe100-stats-health: ffn_fe100_stats.c ffn_fe100_stats.h ffn_fe100_stats_probe.c test_fe100_stats_health.c
	$(CC) $(CFLAGS) ffn_fe100_stats.c test_fe100_stats_health.c -Wl,--wrap=getsockopt -o $@

ffn-fe100-punt-probe: ffn_fe100_punt_probe.c libffn-packet.so
	$(CC) $(CFLAGS) ffn_fe100_punt_probe.c -L. -lffn-packet -o $@

libffn-hwio.so: ffn_hwio.c
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now ffn_hwio.c -o $@

INLINE = ../dpfwd/ffn_inline_adapter.c ../dpfwd/ffn_dp_engine.c ../dpfwd/ffn_dp_dlp.c
libffn-inline.so: $(INLINE) $(wildcard ../dpfwd/*.h)
	$(CC) $(CFLAGS) -Wno-unused-parameter -fPIC -shared -Wl,-z,relro,-z,now $(INLINE) -o $@

test-scan.so: test_scan.c
	$(CC) $(CFLAGS) -fPIC -shared test_scan.c -o $@

test-hwio: ffn_hwio.c test_hwio.c
	$(CC) $(CFLAGS) ffn_hwio.c test_hwio.c -Wl,--wrap=open,--wrap=close,--wrap=ioctl -o $@

test: libffn-packet.so libffn-inline.so test-scan.so test-hwio test-fe100-punt test-fe100-stats test-fe100-stats-health
	./test-hwio
	./test-fe100-punt
	./test-fe100-stats
	./test-fe100-stats-health
	FFN_PACKET_LIB=$(CURDIR)/libffn-packet.so python3 test_packet.py
	FFN_PACKET_LIB=$(CURDIR)/libffn-packet.so python3 test_fe100_packet.py
	python3 test_aggregate.py
	cd ../debian && python3 -m unittest test_packet_cpus

.PHONY: all test
