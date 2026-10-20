CC ?= cc
CFLAGS ?= -O2 -g -std=gnu11 -Wall -Wextra -Werror -pthread

all: libffn-packet.so libffn-hwio.so libffn-inline.so libffn-fe100-resources.so

# The FFN Protect tunnel worker links the target's libcrypto (OpenSSL 3). On a
# cross build pass PROTECT_CFLAGS/PROTECT_LDFLAGS for the target's headers and
# library (the image builder does); on the host the defaults suffice.
PROTECT_CFLAGS ?=
PROTECT_LDFLAGS ?= -lcrypto
ffn-protect-tunnel: ffn_protect_tunnel.c
	$(CC) $(CFLAGS) $(PROTECT_CFLAGS) -Wno-deprecated-declarations ffn_protect_tunnel.c $(PROTECT_LDFLAGS) -o $@

# The CP owner of one physical port's packet redirect (replaces ffn_physical_forwarding.py).
ffn-portctl: ffn_portctl.c ffn_portctl.h ffn_json.c ffn_json.h
	$(CC) $(CFLAGS) ffn_portctl.c ffn_json.c -o $@

test-portctl: ffn_portctl.c ffn_portctl.h ffn_json.c ffn_json.h test_portctl.c
	$(CC) $(CFLAGS) -Wno-unused-function test_portctl.c -o $@

# The dataplane LLDP agent for front ports (own code; trunk control frames in and out).
ffn-lldpd: ffn_lldpd.c ffn_lldp.c ffn_lldp.h ffn_json.c ffn_json.h
	$(CC) $(CFLAGS) ffn_lldpd.c ffn_lldp.c ffn_json.c -o $@

test-lldp: ffn_lldp.c ffn_lldp.h ffn_json.c ffn_json.h test_lldp.c
	$(CC) $(CFLAGS) -Wno-unused-function test_lldp.c -o $@

# The LACP engine and codec (own code). The shared object exists for the Python
# conformance shim; ffn-portd compiles the engine in directly.
libffn-lacp.so: ffn_lacp.c ffn_lacp.h
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now ffn_lacp.c -o $@

test-lacp: ffn_lacp.c ffn_lacp.h test_lacp.c
	$(CC) $(CFLAGS) ffn_lacp.c test_lacp.c -o $@

# The dataplane port owner: one daemon for every faceplate port's packet path
# (WAN, physical, copper with its observed carrier) and every aggregate (LACP
# engine, member gates, trunk control socket), each under its MP contract.
ffn-portd: ffn_portd.c ffn_lacp.c ffn_lacp.h ffn_json.c ffn_json.h ffn_packet.h libffn-packet.so
	$(CC) $(CFLAGS) ffn_portd.c ffn_lacp.c ffn_json.c -L. -lffn-packet -ldl -o $@

# The CP's SFP+ cage control: expanders, SFF-8472 pages, link mode, transmit check, insertion watcher.
ffn-sfpctl: ffn_sfpctl.c ffn_sfp.c ffn_sfp.h ffn_bcm_client.c ffn_bcm_client.h ffn_hwio.c ffn_json.c ffn_json.h
	$(CC) $(CFLAGS) ffn_sfpctl.c ffn_sfp.c ffn_bcm_client.c ffn_hwio.c ffn_json.c -lm -o $@

test-sfp: ffn_sfp.c ffn_sfp.h ffn_json.c ffn_json.h test_sfp.c
	$(CC) $(CFLAGS) -Wno-unused-function test_sfp.c -lm -o $@

test-protect-tunnel: ffn_protect_tunnel.c test_protect_tunnel.c
	$(CC) $(CFLAGS) $(PROTECT_CFLAGS) -Wno-deprecated-declarations -Wno-unused-function test_protect_tunnel.c $(PROTECT_LDFLAGS) -o $@

libffn-fe100-resources.so: ffn_fe100_resources.c ffn_fe100_resources.h
	$(CC) $(CFLAGS) -fPIC -shared -Wl,-z,relro,-z,now ffn_fe100_resources.c -ldl -o $@

test-fe100-resources: ffn_fe100_resources.c ffn_fe100_resources.h test_fe100_resources.c
	$(CC) $(CFLAGS) test_fe100_resources.c -ldl -o $@

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

test: libffn-packet.so libffn-inline.so libffn-lacp.so test-scan.so test-hwio test-fe100-punt test-fe100-stats test-fe100-stats-health test-fe100-resources test-protect-tunnel test-portctl test-lldp test-sfp test-lacp
	./test-protect-tunnel
	./test-fe100-resources
	./test-hwio
	./test-fe100-punt
	./test-fe100-stats
	./test-fe100-stats-health
	./test-portctl
	./test-lldp
	./test-sfp
	./test-lacp
	cd ../debian && FFN_LACP_ENGINE=native FFN_LACP_LIB=$(CURDIR)/libffn-lacp.so python3 -m unittest test_lacp_native test_lacp_engine
	FFN_PACKET_LIB=$(CURDIR)/libffn-packet.so python3 test_packet.py
	FFN_PACKET_LIB=$(CURDIR)/libffn-packet.so python3 test_fe100_packet.py
	python3 test_aggregate.py
	cd ../debian && python3 -m unittest test_packet_cpus

.PHONY: all test
