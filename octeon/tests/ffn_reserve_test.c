/*
 * Host-side unit test for the ffn_reserve= exclusion arithmetic.
 *
 * The code under test is #included verbatim from the patched
 * arch/mips/cavium-octeon/setup.c, so this tests what actually ships.
 *
 * It replays plat_mem_setup()'s real allocation pattern -- 4 MB chunks,
 * ascending, 1 MB aligned -- and asserts three invariants:
 *
 *   1. no emitted RAM region overlaps any reserved range;
 *   2. emitted RAM + reserved == the chunks fed in (nothing lost, nothing
 *      double-counted);
 *   3. every range reports hit == size, i.e. it was fully excluded.
 *
 * Build: cc -Wall -Wextra -O2 -o ffn_reserve_test ffn_reserve_test.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

typedef uint64_t u64;

#define PAGE_SIZE	4096UL
#define PAGE_MASK	(~(PAGE_SIZE - 1))
#define PAGE_ALIGN(x)	(((x) + PAGE_SIZE - 1) & PAGE_MASK)
#define BOOT_MEM_RAM	1
#define __init
#define __initdata

static int fail;

#define pr_warn(...)	do { printf("  [pr_warn] " __VA_ARGS__); } while (0)
#define pr_info(...)	do { printf("  [pr_info] " __VA_ARGS__); } while (0)
#define pr_err(...)	do { printf("  [pr_err ] " __VA_ARGS__); } while (0)

/* ---- captured emissions --------------------------------------------- */
struct region {
	u64 base;
	u64 size;
};

#define MAX_REGION 4096
static struct region emitted[MAX_REGION];
static int emitted_n;

static void add_memory_region(u64 start, u64 size, long type)
{
	(void)type;
	if (!size)
		return;
	if (emitted_n == MAX_REGION) {
		printf("FAIL: emitted overflow\n");
		exit(1);
	}
	emitted[emitted_n].base = start;
	emitted[emitted_n].size = size;
	emitted_n++;
}

/* ---- command line + memparse stubs --------------------------------- */
static char arcs_cmdline[1024];

static unsigned long long memparse(const char *ptr, char **retptr)
{
	unsigned long long ret = strtoull(ptr, retptr, 0);

	switch (**retptr) {
	case 'G': case 'g': ret <<= 30; (*retptr)++; break;
	case 'M': case 'm': ret <<= 20; (*retptr)++; break;
	case 'K': case 'k': ret <<= 10; (*retptr)++; break;
	default: break;
	}
	return ret;
}

/* max_memory: the 512 MB compile-time default ffn_mem=auto exists to replace */
static unsigned long long max_memory = 512ull << 20;

/* ---- the code under test, verbatim from setup.c --------------------- */
#include "ffn_extract.inc"

/* ---- harness -------------------------------------------------------- */
#define CVMX_PCIE_BAR1_PHYS_BASE (((u64)1 << 32) - ((u64)1 << 28))
#define CVMX_PCIE_BAR1_PHYS_SIZE ((u64)1 << 28)

static void reset(void)
{
	emitted_n = 0;
	ffn_reserve_count = 0;
	memset(ffn_reserve, 0, sizeof(ffn_reserve));
	memset(arcs_cmdline, 0, sizeof(arcs_cmdline));
}

/* Replay the plat_mem_setup() chunk loop over [start, start+len). */
static void run_chunks(u64 start, u64 len)
{
	const u64 chunk = 4 << 20;
	u64 at;

	for (at = start; at < start + len; at += chunk) {
		u64 memory = at;
		u64 size = chunk;
		int ri;

		for (ri = 0; ri < ffn_reserve_count; ri++)
			memory_exclude_range(&ffn_reserve[ri], &memory, &size);
		if (size)
			add_memory_region(memory, size, BOOT_MEM_RAM);
	}
}

static int overlaps(u64 a, u64 alen, u64 b, u64 blen)
{
	return a < b + blen && b < a + alen;
}

static void check(const char *name, u64 start, u64 len)
{
	u64 emitted_bytes = 0, reserved_bytes = 0;
	int i, j, bad = 0;

	for (i = 0; i < emitted_n; i++) {
		emitted_bytes += emitted[i].size;

		/* invariant 1: never hand out a reserved page */
		for (j = 0; j < ffn_reserve_count; j++) {
			if (overlaps(emitted[i].base, emitted[i].size,
				     ffn_reserve[j].base, ffn_reserve[j].size)) {
				printf("  FAIL %s: emitted 0x%llx+0x%llx overlaps reserve 0x%llx+0x%llx\n",
				       name,
				       (unsigned long long)emitted[i].base,
				       (unsigned long long)emitted[i].size,
				       (unsigned long long)ffn_reserve[j].base,
				       (unsigned long long)ffn_reserve[j].size);
				bad = 1;
			}
		}

		/* emitted regions must not overlap each other */
		for (j = i + 1; j < emitted_n; j++) {
			if (overlaps(emitted[i].base, emitted[i].size,
				     emitted[j].base, emitted[j].size)) {
				printf("  FAIL %s: emitted regions %d and %d overlap\n",
				       name, i, j);
				bad = 1;
			}
		}
	}

	/* invariant 3: each range fully excluded */
	for (j = 0; j < ffn_reserve_count; j++) {
		reserved_bytes += ffn_reserve[j].hit;
		if (ffn_reserve[j].hit != ffn_reserve[j].size) {
			printf("  FAIL %s: reserve 0x%llx+0x%llx only hit 0x%llx\n",
			       name,
			       (unsigned long long)ffn_reserve[j].base,
			       (unsigned long long)ffn_reserve[j].size,
			       (unsigned long long)ffn_reserve[j].hit);
			bad = 1;
		}
	}

	/* invariant 2: conservation */
	if (emitted_bytes + reserved_bytes != len) {
		printf("  FAIL %s: emitted 0x%llx + reserved 0x%llx != fed 0x%llx (delta %lld)\n",
		       name, (unsigned long long)emitted_bytes,
		       (unsigned long long)reserved_bytes,
		       (unsigned long long)len,
		       (long long)(emitted_bytes + reserved_bytes - len));
		bad = 1;
	}

	(void)start;
	printf("%-52s %s  (%d regions, reserved %llu KB)\n", name,
	       bad ? "FAIL" : "ok", emitted_n,
	       (unsigned long long)(reserved_bytes >> 10));
	if (bad)
		fail = 1;
}

static void tc(const char *name, const char *cmdline, u64 start, u64 len)
{
	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline), "%s", cmdline);
	ffn_reserve_parse();
	run_chunks(start, len);
	check(name, start, len);
}

int main(void)
{
	printf("=== ffn_reserve= exclusion arithmetic ===\n\n");

	/* the real PA-5220 CP layout: overlay + cpdp ring + pcnet ring */
	tc("real layout, four ranges",
	   "console=ttyS0 ffn_reserve=0x22000000,0x1a4b000;0x28000000,1M;0x29000000,4M rw",
	   0x20300000ULL, 0x10000000ULL);

	/* the single generous block */
	tc("single 128M block",
	   "ffn_reserve=0x22000000,128M rw",
	   0x20300000ULL, 0x10000000ULL);

	/* range exactly equal to one chunk */
	tc("range == one whole 4M chunk",
	   "ffn_reserve=0x24000000,4M",
	   0x24000000ULL, 0x1000000ULL);

	/* range strictly inside a chunk (head + tail split) */
	tc("range inside a chunk (split both sides)",
	   "ffn_reserve=0x24100000,0x100000",
	   0x24000000ULL, 0x400000ULL);

	/* range straddling a chunk boundary */
	tc("range straddling a chunk boundary",
	   "ffn_reserve=0x243ff000,0x2000",
	   0x24000000ULL, 0x800000ULL);

	/* unaligned base and size -- must be aligned outwards */
	tc("unaligned base and size",
	   "ffn_reserve=0x24100123,0x1001",
	   0x24000000ULL, 0x400000ULL);

	/* out of order on the command line -- parser must sort */
	tc("out-of-order ranges (parser sorts)",
	   "ffn_reserve=0x29000000,4M;0x22000000,1M;0x28000000,1M",
	   0x20300000ULL, 0x10000000ULL);

	/* overlapping ranges must not double-count */
	tc("overlapping ranges",
	   "ffn_reserve=0x24000000,2M;0x24100000,2M",
	   0x24000000ULL, 0x800000ULL);

	/* adjacent ranges */
	tc("adjacent ranges",
	   "ffn_reserve=0x24000000,1M;0x24100000,1M",
	   0x24000000ULL, 0x800000ULL);

	/* no parameter at all -- must be a no-op */
	tc("no ffn_reserve= at all", "console=ttyS0 rw",
	   0x20300000ULL, 0x1000000ULL);

	/*
	 * The DP production line (from pan-c2). This asserts the parsed bases and
	 * sizes, not just the invariants, because the whole point of a production
	 * string is that it means what the operator thinks it means.
	 */
	{
		static const struct region want[] = {
			{ 0x400000ULL,   0x400000ULL  },  /* dpsh mailbox window  */
			{ 0x21000000ULL, 0x1000000ULL },  /* kernel staging addr  */
			{ 0x28000000ULL, 0x4000000ULL },  /* dpnet rings          */
		};
		int k;

		printf("\n--- DP production line ---\n");
		reset();
		snprintf(arcs_cmdline, sizeof(arcs_cmdline),
			 "ffn_reserve=0x400000,4M;0x21000000,16M;0x28000000,64M");
		ffn_reserve_parse();

		if (ffn_reserve_count != 3) {
			printf("FAIL: parsed %d ranges, want 3\n", ffn_reserve_count);
			fail = 1;
		}
		for (k = 0; k < ffn_reserve_count && k < 3; k++) {
			int ok = ffn_reserve[k].base == want[k].base &&
				 ffn_reserve[k].size == want[k].size;

			printf("range %d: 0x%llx+0x%llx %s\n", k,
			       (unsigned long long)ffn_reserve[k].base,
			       (unsigned long long)ffn_reserve[k].size,
			       ok ? "ok" : "FAIL");
			if (!ok)
				fail = 1;
		}
		/*
		 * Range 0 must end exactly at 0x800000, the kernel link address
		 * (see the CP's own boot map: "kernel data and code" @ 0x800000).
		 * Adjacent is correct; one byte over would reserve kernel text.
		 */
		if (ffn_reserve_count > 0 &&
		    ffn_reserve[0].base + ffn_reserve[0].size != 0x800000ULL) {
			printf("FAIL: range 0 does not end at the kernel link address 0x800000\n");
			fail = 1;
		}
		run_chunks(0x400000ULL, 0x2BC00000ULL);
		check("DP production line invariants",
		      0x400000ULL, 0x2BC00000ULL);
	}

	/* ---- parser-only cases (expected to warn, not crash) ---- */
	printf("\n--- malformed input (must warn, never crash) ---\n");
	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline), "ffn_reserve=0x28000000");
	ffn_reserve_parse();
	printf("missing size          -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 0 ? "ok" : "FAIL");
	if (ffn_reserve_count != 0)
		fail = 1;

	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline), "ffn_reserve=0x28000000,0");
	ffn_reserve_parse();
	printf("zero size             -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 0 ? "ok" : "FAIL");
	if (ffn_reserve_count != 0)
		fail = 1;

	/* nine well-separated ranges: the cap must bite at 8, and because they
	 * do not touch, coalescing must not reduce them further */
	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline),
		 "ffn_reserve=0x100000,4K;0x200000,4K;0x300000,4K;0x400000,4K;"
		 "0x500000,4K;0x600000,4K;0x700000,4K;0x800000,4K;0x900000,4K");
	ffn_reserve_parse();
	printf("more than max ranges  -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 8 ? "ok" : "FAIL");
	if (ffn_reserve_count != 8)
		fail = 1;

	/* coalescing must collapse a fully-overlapping set to one range */
	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline),
		 "ffn_reserve=0x1,1M;0x2,1M;0x3,1M;0x4,1M");
	ffn_reserve_parse();
	printf("overlapping set coalesces -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 1 ? "ok" : "FAIL");
	if (ffn_reserve_count != 1)
		fail = 1;

	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline),
		 "ffn_reserve=0x28000000,1M other=1 rw");
	ffn_reserve_parse();
	printf("stops at whitespace   -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 1 ? "ok" : "FAIL");
	if (ffn_reserve_count != 1)
		fail = 1;

	/*
	 * A malformed entry must cost ONLY itself.
	 *
	 * Regression test for the failure mode pan-c2 hit on the DP: a bad entry
	 * used to abort the whole parse, so a typo in the middle kept the ranges
	 * before it, silently dropped every range after it, and left a boot line
	 * that still read exactly as intended. The lost regions then corrupt
	 * later with nothing pointing back at the cause.
	 */
	printf("\n--- a bad entry must not take its siblings down ---\n");
	{
		int k, saw28 = 0, saw29 = 0;

		reset();
		snprintf(arcs_cmdline, sizeof(arcs_cmdline),
			 "ffn_reserve=0x28000000,1M;bogus;0x29000000,4M");
		ffn_reserve_parse();
		for (k = 0; k < ffn_reserve_count; k++) {
			if (ffn_reserve[k].base == 0x28000000ULL)
				saw28 = 1;
			if (ffn_reserve[k].base == 0x29000000ULL)
				saw29 = 1;
		}
		printf("bad entry in the middle -> %d range(s), 0x28000000 %s, 0x29000000 %s : %s\n",
		       ffn_reserve_count, saw28 ? "kept" : "LOST",
		       saw29 ? "kept" : "LOST",
		       (ffn_reserve_count == 2 && saw28 && saw29) ? "ok" : "FAIL");
		if (!(ffn_reserve_count == 2 && saw28 && saw29))
			fail = 1;
	}

	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline),
		 "ffn_reserve=;0x28000000,1M");
	ffn_reserve_parse();
	printf("leading separator     -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 1 ? "ok" : "FAIL");
	if (ffn_reserve_count != 1)
		fail = 1;

	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline),
		 "ffn_reserve=0x28000000,;0x29000000,4M");
	ffn_reserve_parse();
	printf("empty size mid-list   -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 1 &&
	       ffn_reserve[0].base == 0x29000000ULL ? "ok" : "FAIL");
	if (!(ffn_reserve_count == 1 && ffn_reserve[0].base == 0x29000000ULL))
		fail = 1;

	/*
	 * A derived range must actually cover the payload it was derived from.
	 * pan-c2's coverage invariant, which catches the class of bug rather
	 * than one instance: a hardcoded size that has fallen behind a grown
	 * payload must FAIL here rather than boot and corrupt.
	 */
	printf("\n--- derived size must cover its payload ---\n");
	{
		struct payload {
			const char *what;
			const char *cmdline;
			u64 base;
			u64 bytes;	/* real payload size */
			int want_ok;
		};
		static const struct payload pl[] = {
			{ "overlay 26.25 MiB, derived 27M",
			  "ffn_reserve=0x22000000,27M", 0x22000000ULL, 27523072ULL, 1 },
			{ "overlay 26.25 MiB, hardcoded 16M (stale)",
			  "ffn_reserve=0x22000000,16M", 0x22000000ULL, 27523072ULL, 0 },
			{ "overlay 19.1 MiB, derived 20M",
			  "ffn_reserve=0x22000000,20M", 0x22000000ULL, 19998720ULL, 1 },
		};
		unsigned k;

		for (k = 0; k < sizeof(pl) / sizeof(pl[0]); k++) {
			int covers;

			reset();
			snprintf(arcs_cmdline, sizeof(arcs_cmdline), "%s",
				 pl[k].cmdline);
			ffn_reserve_parse();
			covers = ffn_reserve_count == 1 &&
				 ffn_reserve[0].base <= pl[k].base &&
				 ffn_reserve[0].base + ffn_reserve[0].size >=
					 pl[k].base + pl[k].bytes;
			printf("%-42s covers=%d want=%d %s\n", pl[k].what,
			       covers, pl[k].want_ok,
			       covers == pl[k].want_ok ? "ok" : "FAIL");
			if (covers != pl[k].want_ok)
				fail = 1;
		}
	}

	/* a base that is not in DRAM must be reported, not silently dropped */
	printf("\n--- base not in DRAM (must report a short hit) ---\n");
	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline), "ffn_reserve=0x900000000,1M");
	ffn_reserve_parse();
	run_chunks(0x20300000ULL, 0x1000000ULL);
	printf("hit=0x%llx size=0x%llx -> %s\n",
	       (unsigned long long)ffn_reserve[0].hit,
	       (unsigned long long)ffn_reserve[0].size,
	       ffn_reserve[0].hit == 0 ? "ok (warned)" : "FAIL");
	if (ffn_reserve[0].hit != 0)
		fail = 1;

	/*
	 * Repeated ffn_reserve= : the u-boot-safe form.
	 *
	 * ';' is u-boot's command separator, and this boot line is executed by
	 * u-boot, so an unescaped one truncates the line and silently drops
	 * every argument after it -- later ranges, and mem=/pktbuf=/wqe= too.
	 * Repetition needs no separator, so it cannot be damaged in transit.
	 */
	printf("\n--- repeated ffn_reserve= (no ';' needed) ---\n");
	{
		int k, saw22 = 0, saw28 = 0, saw29 = 0;

		reset();
		snprintf(arcs_cmdline, sizeof(arcs_cmdline),
			 "console=ttyS0 ffn_reserve=0x22000000,27M "
			 "ffn_reserve=0x28000000,1M ffn_reserve=0x29000000,4M rw");
		ffn_reserve_parse();
		for (k = 0; k < ffn_reserve_count; k++) {
			if (ffn_reserve[k].base == 0x22000000ULL) saw22 = 1;
			if (ffn_reserve[k].base == 0x28000000ULL) saw28 = 1;
			if (ffn_reserve[k].base == 0x29000000ULL) saw29 = 1;
		}
		printf("three separate params -> %d range(s) %s\n",
		       ffn_reserve_count,
		       (ffn_reserve_count == 3 && saw22 && saw28 && saw29)
		       ? "ok" : "FAIL");
		if (!(ffn_reserve_count == 3 && saw22 && saw28 && saw29))
			fail = 1;
		run_chunks(0x20300000ULL, 0x10000000ULL);
		check("repeated-param invariants", 0x20300000ULL, 0x10000000ULL);
	}

	/* mixing both forms must also work */
	reset();
	snprintf(arcs_cmdline, sizeof(arcs_cmdline),
		 "ffn_reserve=0x22000000,1M;0x24000000,1M ffn_reserve=0x28000000,1M");
	ffn_reserve_parse();
	printf("mixed ';' and repetition -> %d range(s) %s\n", ffn_reserve_count,
	       ffn_reserve_count == 3 ? "ok" : "FAIL");
	if (ffn_reserve_count != 3)
		fail = 1;

	/* ---- ffn_mem=auto ------------------------------------------------ */
	printf("\n--- ffn_mem=auto sizing ---\n");
	{
		const u64 M = 1ull << 20;
		struct mcase {
			const char *what;
			const char *cmdline;
			u64 avail;
			u64 pre;	/* max_memory before the call */
			u64 want;	/* expected max_memory after */
		};
		/*
		 * Expected reserve, from the spec rather than copied from the
		 * code: 2*(declared pools) + margin, margin = max(256M, avail/32).
		 *
		 * The small pktbuf=8192,2048 wqe=256,128 is what FFN actually
		 * boots on BOTH planes today -- the DP declares the same
		 * bring-up values as the CP because its packet engine has never
		 * initialised. The large values are the VENDOR's DP line, kept
		 * as a case because that is what the derivation must grow to
		 * once the engine is brought up.
		 */
#define POOLS_SMALL	(8192ull * 2048 + 256ull * 128)
#define POOLS_VENDOR	(86016ull * 2048 + 856600ull * 128)
#define MARGIN(avail)	(((avail) >> 4) < 256 * M ? 256 * M : ((avail) >> 4))
#define RESERVE(avail, pools)	(2 * (pools) + MARGIN(avail))
		struct mcase mc[] = {
			{ "CP line (small pools), 8 GiB",
			  "ffn_mem=auto pktbuf=8192,2048 wqe=256,128",
			  8192 * M, 512 * M,
			  8192 * M - RESERVE(8192 * M, POOLS_SMALL) },
			{ "FFN DP line (small pools), 32 GiB",
			  "ffn_mem=auto pktbuf=8192,2048 wqe=256,128",
			  32768 * M, 512 * M,
			  32768 * M - RESERVE(32768 * M, POOLS_SMALL) },
			{ "vendor DP line (large pools), 32 GiB",
			  "ffn_mem=auto pktbuf=86016,2048 wqe=856600,128",
			  32768 * M, 512 * M,
			  32768 * M - RESERVE(32768 * M, POOLS_VENDOR) },
			{ "explicit reserve wins",
			  "ffn_mem=auto,2G pktbuf=86016,2048 wqe=856600,128",
			  32768 * M, 512 * M, 32768 * M - 2048 * M },
			{ "no ffn_mem= -> untouched",
			  "console=ttyS0 rw", 8192 * M, 512 * M, 512 * M },
			{ "mem= already applied -> ignored",
			  "ffn_mem=auto", 8192 * M, 8192 * M, 8192 * M },
			{ "too little available -> keeps default",
			  "ffn_mem=auto pktbuf=86016,2048 wqe=856600,128",
			  256 * M, 512 * M, 512 * M },
			{ "no pools declared -> margin only",
			  "ffn_mem=auto", 8192 * M, 512 * M,
			  8192 * M - MARGIN(8192 * M) },
			{ "margin scales with DRAM, not pools",
			  "ffn_mem=auto pktbuf=8192,2048 wqe=256,128",
			  65536 * M, 512 * M,
			  65536 * M - RESERVE(65536 * M, POOLS_SMALL) },
		};
		unsigned k;

		for (k = 0; k < sizeof(mc) / sizeof(mc[0]); k++) {
			int ok;

			memset(arcs_cmdline, 0, sizeof(arcs_cmdline));
			snprintf(arcs_cmdline, sizeof(arcs_cmdline), "%s",
				 mc[k].cmdline);
			max_memory = mc[k].pre;
			ffn_mem_auto(mc[k].avail);
			ok = max_memory == mc[k].want;
			printf("%-38s max_memory=%llu MB want=%llu MB %s\n",
			       mc[k].what,
			       (unsigned long long)(max_memory >> 20),
			       (unsigned long long)(mc[k].want >> 20),
			       ok ? "ok" : "FAIL");
			if (!ok)
				fail = 1;
		}
		max_memory = 512ull << 20;
	}

	printf("\n%s\n", fail ? "*** FAILURES ***" : "all checks passed");
	return fail;
}
