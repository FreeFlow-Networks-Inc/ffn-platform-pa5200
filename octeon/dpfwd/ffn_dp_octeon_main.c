/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_octeon_main.c -- the dataplane forwarder, running on real OCTEON III.
 *
 * The same engine as ffn_dp_afpacket_main.c drives here; only the I/O backend
 * differs. That file exists to exercise policy, classify, flow cache, L3 and
 * verdicts against veth pairs with no hardware. This one runs it against PKI,
 * SSO and PKO3 on the DP's 40 cores.
 *
 * THE ENTRY POINT IS appmain(), NOT main(). That is not a style choice: in a
 * CVMX_BUILD_FOR_LINUX_USER build the SDK owns main(). Its runtime brings up the
 * per-core CVMX state, maps hardware, and only then calls
 *
 *     result = appmain(argc, argv);        [cvmx-app-init-linux.c:398]
 *
 * Define main() instead and you get a duplicate-symbol link failure, or worse a
 * program that runs with none of the CVMX setup done.
 *
 * TWO KERNEL PREREQUISITES, both of which fail in ways that do not name
 * themselves. Check these first if this program dies early:
 *
 *   1. /proc/octeon_info must exist -- octeon/kctl/ffn_octeon_info.c. Without
 *      it cvmx_user_app_init() calls exit(-1) from inside
 *      cvmx_sysinfo_linux_userspace_initialize(), before appmain() is reached,
 *      so the program produces a perror line and nothing else.
 *
 *   2. CvmMemCtl[xkmemenau,xkioenau] must be set -- octeon/kctl/ffn_xkphys.c.
 *      Every CSR access in this build is an inline `ld`/`sd` at an XKPHYS
 *      address, so without it the first register read is a SIGSEGV with no
 *      message at all.
 *
 * Expect one harmless line on stderr at startup:
 *     sysmips(MIPS_CAVIUM_XKPHYS_WRITE) failed: Invalid argument
 * The SDK asks the kernel for XKPHYS access per process; upstream has no such
 * command, and ffn_xkphys.c has already granted it machine-wide. See
 * compat/sys/sysmips.h.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>

#include "ffn_dp_abi.h"
#include "ffn_dp_oct.h"
#include "ffn_dp_io_octeon.h"
#include "ffn_dp_io_octeon3.h"

#define MAX_PORTS 8

static volatile sig_atomic_t g_stop;

static void on_signal(int sig)
{
	(void)sig;
	g_stop = 1;
}

static void usage(const char *me)
{
	fprintf(stderr,
		"usage: %s [--probe] [-p ipd_port]... [-v vsys]\n"
		"          [-d drop|forward|local] [-s stats_sec] [-c stop_after_pkts]\n"
		"\n"
		"  --probe  print what the SDK helper believes about each interface\n"
		"           (mode, port count, ipd_port, PKO3 queue) and exit. Start\n"
		"           here: a missing PKO3 queue means a zero-port interface.\n"
		"  -p N   add a port by IPD port number (repeatable, max %d).\n"
		"         Port INDEX is the order given, and that index is what a\n"
		"         policy's `egress` field selects -- not the IPD number.\n"
		"  -v N   vsys tag applied to frames (default 1)\n"
		"  -d D   verdict when no rule matches (default drop)\n"
		"  -s N   print stats every N seconds (0 = only at exit)\n"
		"  -c N   stop after N packets received (0 = until signalled)\n",
		me, MAX_PORTS);
}

int appmain(int argc, const char *argv[]);

int appmain(int argc, const char *argv[])
{
	int ipd[MAX_PORTS];
	int n_ipd = 0;
	unsigned vsys = 1;
	int stats_sec = 5;
	unsigned long long stop_after = 0;
	int default_dec = FP_DROP_W;
	int probe = 0;
	int i;

	/*
	 * Line-buffer stdout before anything is printed.
	 *
	 * Redirected to a file, stdout is block-buffered, and this program is
	 * normally stopped with a signal -- so every progress line written before
	 * the first fflush() is lost, and the log looks as though execution never
	 * reached them. That cost a debugging round: the app was hanging in a
	 * known place and the log showed nothing after CVMX's own output, which
	 * reads exactly like a much earlier failure.
	 */
	setvbuf(stdout, NULL, _IOLBF, 0);

	for (i = 1; i < argc; i++) {
		const char *a = argv[i];

		if (!strcmp(a, "--probe")) {
			probe = 1;
		} else if (!strcmp(a, "-p") && i + 1 < argc) {
			if (n_ipd >= MAX_PORTS) {
				fprintf(stderr, "too many ports (max %d)\n", MAX_PORTS);
				return 1;
			}
			ipd[n_ipd++] = (int)strtol(argv[++i], NULL, 0);
		} else if (!strcmp(a, "-v") && i + 1 < argc) {
			vsys = (unsigned)strtoul(argv[++i], NULL, 0);
		} else if (!strcmp(a, "-s") && i + 1 < argc) {
			stats_sec = (int)strtol(argv[++i], NULL, 0);
		} else if (!strcmp(a, "-c") && i + 1 < argc) {
			stop_after = strtoull(argv[++i], NULL, 0);
		} else if (!strcmp(a, "-d") && i + 1 < argc) {
			const char *d = argv[++i];

			if (!strcmp(d, "drop"))
				default_dec = FP_DROP_W;
			else if (!strcmp(d, "forward"))
				default_dec = FP_FORWARD_W;
			else if (!strcmp(d, "local"))
				default_dec = FP_LOCAL_W;
			else {
				fprintf(stderr, "bad -d %s\n", d);
				return 1;
			}
		} else {
			usage(argv[0]);
			return 1;
		}
	}

	if (!oct_backend_available()) {
		fprintf(stderr, "built without CVMX: %s\n", oct_backend_name());
		return 1;
	}

	/* --probe runs BEFORE the port check, because the whole point of it is to
	 * find out which ports exist. Requiring -p first would mean guessing the
	 * answer in order to ask the question.
	 *
	 * It still needs the CVMX runtime up, which by this point it is: the SDK
	 * calls appmain() only after cvmx_user_app_init() has returned.
	 */
	if (probe) {
		cvmx3_probe_interfaces(stdout);
		return 0;
	}

	if (n_ipd == 0) {
		fprintf(stderr, "no ports given; -p is required\n");
		usage(argv[0]);
		return 1;
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	/* Report the generation the CHIP claims, not the one we were built for.
	 * oct_detect_gen() asks the hardware (OCTEON_FEATURE_CN78XX_WQE); a
	 * mismatch here against OCTEON_MODEL means the binary is for another
	 * part and every register offset below is suspect.
	 */
	printf("ffn-dp-octeon: backend %s\n", oct_backend_name());

	struct oct_ctx oct;

	oct_ctx_init(&oct, &OCT_HW_CVMX3, NULL);

	for (i = 0; i < n_ipd; i++) {
		char name[32];

		snprintf(name, sizeof(name), "ipd%d", ipd[i]);
		/* pko_queue -1: cvmx3_hw_init resolves the real descriptor queue
		 * with cvmx_pko3_get_queue_base(ipd_port). A queue number invented
		 * here would be accepted and then transmit into the wrong one.
		 */
		int rc = oct_add_port(&oct, name, ipd[i], -1, (uint8_t)vsys);

		if (rc < 0) {
			fprintf(stderr, "oct_add_port(ipd %d): %d\n", ipd[i], rc);
			return 1;
		}
		printf("  port %d = ipd %d  vsys %u\n", rc, ipd[i], vsys);
	}

	struct dp_ctx dp;
	int rc = dp_init(&dp, &OCT_IO, &oct, 0);

	if (rc != DP_OK) {
		fprintf(stderr, "dp_init: %s\n", dp_strerror(rc));
		return 1;
	}
	dp.default_decision = default_dec;

	/* Same handshake/bank machinery the AF_PACKET build uses; ordinary
	 * memory here rather than a PCIe BAR, because nothing on the far side
	 * is reading it yet.
	 */
	size_t rsz = FFN_DP_OFF_BANK0 + (4u << 20);
	void *region = calloc(1, rsz);

	if (!region) {
		fprintf(stderr, "region alloc failed\n");
		return 1;
	}
	rc = dp_region_attach(&dp, region, rsz, 1);
	if (rc != DP_OK) {
		fprintf(stderr, "region attach: %s\n", dp_strerror(rc));
		return 1;
	}

	dp_set_state(&dp, DP_STATE_READY);
	printf("ffn-dp-octeon: ready, %d port(s), default=%s\n",
	       n_ipd,
	       default_dec == FP_DROP_W ? "drop" :
	       default_dec == FP_FORWARD_W ? "forward" : "local");
	fflush(stdout);

	time_t last = time(NULL);

	while (!g_stop) {
		dp_poll_once(&dp);
		if (stop_after && dp.stat_rx >= stop_after)
			break;
		if (stats_sec) {
			time_t now = time(NULL);

			if (now - last >= stats_sec) {
				last = now;
				printf("rx=%llu tx=%llu tx_fail=%llu fwd=%llu "
				       "drop=%llu parse_err=%llu\n",
				       (unsigned long long)dp.stat_rx,
				       (unsigned long long)dp.stat_tx,
				       (unsigned long long)dp.stat_tx_fail,
				       (unsigned long long)dp.stat_forward,
				       (unsigned long long)dp.stat_drop,
				       (unsigned long long)dp.stat_parse_err);
				fflush(stdout);
			}
		}
	}

	printf("\n--- final ---\n");
	oct_dump_stats(&oct, stdout);
	printf("dp: rx=%llu tx=%llu tx_fail=%llu fwd=%llu drop=%llu "
	       "parse_err=%llu\n",
	       (unsigned long long)dp.stat_rx,
	       (unsigned long long)dp.stat_tx,
	       (unsigned long long)dp.stat_tx_fail,
	       (unsigned long long)dp.stat_forward,
	       (unsigned long long)dp.stat_drop,
	       (unsigned long long)dp.stat_parse_err);

	/* bug_double_dispose must be zero. It counts a buffer disposed twice,
	 * which on this path means a packet's data was returned to its aura and
	 * then returned again -- silent pool corruption that shows up much later
	 * as an unrelated failure, so it is worth saying loudly here.
	 */
	if (oct.bug_double_dispose)
		printf("BUG: double dispose count = %llu\n",
		       (unsigned long long)oct.bug_double_dispose);

	return 0;
}
