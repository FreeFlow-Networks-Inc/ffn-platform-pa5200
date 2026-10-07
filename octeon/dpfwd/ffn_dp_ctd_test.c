// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * ffn_dp_ctd_test.c -- tests for the content inspection engine.
 *
 * Standalone on purpose: links only ffn_dp_ctd.c and ffn_dp_aho.c, no
 * forwarder and no packet-I/O backend, which is the whole claim the module
 * makes about itself. Runs natively and under qemu-mips64.
 */

#include <stdio.h>
#include <string.h>

#include "ffn_dp_engine.h"
#include "ffn_dp_aho.h"
#include "ffn_dp_ctd.h"

static int pass, fail;

static void chk(int cond, const char *what)
{
	if (cond) {
		pass++;
	} else {
		fail++;
		printf("  FAIL: %s\n", what);
	}
}

/* A packet as the dispatcher would hand it over. */
static void mkctx(struct dp_engine_ctx *c, const char *payload, int dir,
		  uint16_t *st)
{
	memset(c, 0, sizeof(*c));
	c->payload     = (const uint8_t *)payload;
	c->payload_len = (uint32_t)strlen(payload);
	c->direction   = (uint8_t)dir;
	c->l4_proto    = 6;
	c->dport       = 80;
	c->scan_state  = st;
}

/* ---- verdicts --------------------------------------------------------- */

static void test_verdicts(void)
{
	struct dp_ctd d;
	struct dp_engine_ctx c;

	printf("verdicts\n");
	dp_ctd_init(&d, 0);
	chk(dp_ctd_add(&d, "noisy",  (const uint8_t *)"ALERTME", 7,
		       DP_EV_ALERT, DP_DIR_UNKNOWN) == 0, "add alert sig");
	chk(dp_ctd_add(&d, "nasty",  (const uint8_t *)"BLOCKME", 7,
		       DP_EV_BLOCK, DP_DIR_UNKNOWN) == 1, "add block sig");
	chk(dp_ctd_add(&d, "fatal",  (const uint8_t *)"KILLME",  6,
		       DP_EV_RESET, DP_DIR_UNKNOWN) == 2, "add reset sig");

	mkctx(&c, "clean traffic", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "scan before commit finds none");

	chk(dp_ctd_commit(&d) == DP_CTD_OK, "commit");
	chk(dp_ctd_commit(&d) == DP_CTD_OK, "commit is idempotent");

	mkctx(&c, "nothing to see", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "clean traffic passes");

	mkctx(&c, "xx ALERTME xx", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_ALERT, "alert");
	chk(c.hit_rule && !strcmp(c.hit_rule, "noisy"), "names the signature");
	chk(c.hit_offset == 3, "reports the FIRST byte of the match");

	/* Worst wins, whichever order they appear in the packet. */
	mkctx(&c, "ALERTME then BLOCKME", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK, "block beats alert");
	mkctx(&c, "BLOCKME then ALERTME", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK, "order does not matter");

	mkctx(&c, "KILLME now", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_RESET, "reset");

	chk(d.sig[0].hits > 0 && d.sig[1].hits > 0, "per-signature hit counts");
	chk(dp_ctd_mem_use(&d) > 0, "mem_use reports something");

	dp_ctd_reset(&d);
	chk(d.count == 0 && d.built == 0 && d.ac == NULL, "reset clears");
}

/* ---- direction -------------------------------------------------------- */

static void test_direction(void)
{
	struct dp_ctd d;
	struct dp_engine_ctx c;

	printf("direction scoping\n");
	dp_ctd_init(&d, 0);
	dp_ctd_add(&d, "leaving", (const uint8_t *)"SECRET", 6,
		   DP_EV_BLOCK, DP_DIR_EGRESS);
	dp_ctd_commit(&d);

	mkctx(&c, "the SECRET value", DP_DIR_EGRESS, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK, "fires on egress");

	mkctx(&c, "the SECRET value", DP_DIR_INGRESS, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "silent on ingress");

	/* Matches the DLP engine's rule: filter only when both sides are known,
	 * so an unknown direction does not suppress detection. */
	mkctx(&c, "the SECRET value", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK, "unknown direction still fires");

	dp_ctd_reset(&d);
}

/* ---- streaming -------------------------------------------------------- */

static void test_streaming(void)
{
	struct dp_ctd d;
	struct dp_engine_ctx c;
	uint16_t st = 0;

	printf("streaming across packets\n");
	dp_ctd_init(&d, 0);
	dp_ctd_add(&d, "split", (const uint8_t *)"ATTACK", 6,
		   DP_EV_BLOCK, DP_DIR_UNKNOWN);
	dp_ctd_commit(&d);

	/* Neither half contains the signature. */
	mkctx(&c, "xxxATT", DP_DIR_UNKNOWN, &st);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "first half alone: no match");
	chk(st != 0, "state advanced into the pattern");

	mkctx(&c, "ACKyyy", DP_DIR_UNKNOWN, &st);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK, "second half completes it");

	/* Without carried state the same two packets must NOT match -- this is
	 * what proves the previous result came from streaming and not from the
	 * matcher seeing the whole string some other way. */
	mkctx(&c, "xxxATT", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "stateless: first half");
	mkctx(&c, "ACKyyy", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "stateless: no cross-packet match");

	dp_ctd_reset(&d);
}

/* ---- truncation must break the stream --------------------------------- */
/*
 * The subtle one. If dp_engine_scan() clipped the packet, the bytes after the
 * clip were never scanned, so the next packet is not adjacent to what was.
 * Carrying state over that gap would report a signature the traffic never
 * contained -- a false positive on a firewall, which is the expensive kind.
 */
static void test_truncation_breaks_stream(void)
{
	struct dp_ctd d;
	struct dp_engine_ctx c;
	uint16_t st = 0;
	uint64_t broken_before;

	printf("truncation breaks the stream\n");
	dp_ctd_init(&d, 0);
	dp_ctd_add(&d, "split", (const uint8_t *)"ATTACK", 6,
		   DP_EV_BLOCK, DP_DIR_UNKNOWN);
	dp_ctd_commit(&d);

	broken_before = d.stat_streams_broken;

	mkctx(&c, "xxxATT", DP_DIR_UNKNOWN, &st);
	c.truncated = 1;                /* dispatcher clipped this packet */
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE, "clipped first half: no match");
	chk(st == 0, "state reset to the root after a clip");
	chk(d.stat_streams_broken == broken_before + 1, "the break is counted");

	mkctx(&c, "ACKyyy", DP_DIR_UNKNOWN, &st);
	chk(dp_ctd_scan(&c, &d) == DP_EV_NONE,
	    "no spliced match across the gap");

	dp_ctd_reset(&d);
}

/* ---- a state left by a previous pattern set --------------------------- */

static void test_stale_state(void)
{
	struct dp_ctd d;
	struct dp_engine_ctx c;
	uint16_t st;

	printf("stale state from a previous pattern set\n");
	dp_ctd_init(&d, 0);
	dp_ctd_add(&d, "sig", (const uint8_t *)"BOOM", 4,
		   DP_EV_BLOCK, DP_DIR_UNKNOWN);
	dp_ctd_commit(&d);

	st = 60000;                     /* far beyond this small automaton */
	mkctx(&c, "zz BOOM zz", DP_DIR_UNKNOWN, &st);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK,
	    "recovers from an out-of-range state and still matches");
	chk(st < 60000, "state replaced with a valid one");

	dp_ctd_reset(&d);
}

/* ---- config ----------------------------------------------------------- */

static void test_config(void)
{
	struct dp_ctd d;
	struct dp_engine_ctx c;

	printf("config grammar\n");
	dp_ctd_init(&d, 0);

	chk(dp_ctd_config_line(&d, "dp.dlp.rule.x", "whatever") == 0,
	    "another engine's key is not ours");
	chk(dp_ctd_config_line(&d, "dp.ctd.case", "fold") == 1, "case=fold");
	chk(dp_ctd_config_line(&d, "dp.ctd.case", "sideways") == -1,
	    "bad case value rejected");

	chk(dp_ctd_config_line(&d, "dp.ctd.sig.evil",
			       "block:any:EVILSTRING") == 1, "good sig line");

	/* Patterns contain colons constantly; splitting on all of them would
	 * corrupt most real signatures. */
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.url",
			       "alert:egress:http://bad.example/x") == 1,
	    "pattern containing colons survives");

	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bin",
			       "block:any:\\x00\\xffZ") == 1, "hex escapes");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bs",
			       "block:any:a\\\\b") == 1, "backslash escape");

	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bad1",
			       "explode:any:X") == -1, "unknown action");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bad2",
			       "block:sideways:X") == -1, "unknown direction");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bad3",
			       "block:any:a\\qb") == -1, "unknown escape refused");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bad4",
			       "block:any:a\\x9") == -1, "truncated hex refused");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bad5", "block:any:") == -1,
	    "empty pattern refused");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.bad6", "block") == -1,
	    "missing fields refused");
	chk(dp_ctd_config_line(&d, "dp.ctd.sig.", "block:any:X") == -1,
	    "empty name refused");

	chk(dp_ctd_config_line(&d, "dp.ctd.case", "sensitive") == -1,
	    "case change after the automaton exists is refused");

	chk(dp_ctd_commit(&d) == DP_CTD_OK, "commit the configured set");
	chk(d.count == 4, "exactly the four good signatures loaded");

	/* case=fold was set before any signature, so it applies. */
	mkctx(&c, "xx evilstring xx", DP_DIR_UNKNOWN, NULL);
	chk(dp_ctd_scan(&c, &d) == DP_EV_BLOCK, "case folding is in effect");

	{
		const char bin[] = { 'q', 0x00, (char)0xff, 'Z', 'q' };
		struct dp_engine_ctx b;
		memset(&b, 0, sizeof(b));
		b.payload = (const uint8_t *)bin;
		b.payload_len = sizeof(bin);
		chk(dp_ctd_scan(&b, &d) == DP_EV_BLOCK,
		    "hex-escaped binary signature matches real bytes");
	}

	chk(dp_ctd_add(&d, "late", (const uint8_t *)"X", 1,
		       DP_EV_ALERT, DP_DIR_UNKNOWN) == DP_CTD_LOCKED,
	    "add after commit refused");

	dp_ctd_reset(&d);
}

/* ---- lifecycle and limits --------------------------------------------- */

static void test_limits(void)
{
	struct dp_ctd d;
	char name[DP_CTD_NAME_MAX + 16];
	char pat[32];
	int i, rc = DP_CTD_OK;

	printf("limits\n");
	dp_ctd_init(&d, 0);
	chk(dp_ctd_commit(&d) == DP_CTD_EMPTY, "commit with no signatures");
	chk(dp_ctd_add(&d, "x", (const uint8_t *)"y", 0,
		       DP_EV_ALERT, DP_DIR_UNKNOWN) == DP_CTD_BADARG,
	    "zero-length pattern refused");
	chk(dp_ctd_add(&d, "x", (const uint8_t *)"y", 1,
		       99, DP_DIR_UNKNOWN) == DP_CTD_BADARG,
	    "nonsense action refused");
	chk(dp_ctd_add(&d, "x", (const uint8_t *)"y", 1,
		       DP_EV_ALERT, 99) == DP_CTD_BADARG,
	    "nonsense direction refused");

	for (i = 0; i < DP_CTD_SIG_MAX + 4; i++) {
		snprintf(name, sizeof(name), "sig%04d", i);
		snprintf(pat,  sizeof(pat),  "pattern-%04d", i);
		rc = dp_ctd_add(&d, name, (const uint8_t *)pat,
				(uint32_t)strlen(pat), DP_EV_ALERT,
				DP_DIR_UNKNOWN);
		if (rc < 0)
			break;
	}
	chk(rc == DP_CTD_FULL && i == DP_CTD_SIG_MAX,
	    "fills to DP_CTD_SIG_MAX then refuses");
	chk(dp_ctd_commit(&d) == DP_CTD_OK, "a full set still compiles");
	printf("    %u signatures -> %llu KiB automaton\n",
	       d.count, (unsigned long long)(dp_ctd_mem_use(&d) / 1024));

	/* An over-long name is truncated, not rejected: losing a detection over
	 * a cosmetic log field would be the wrong trade. */
	dp_ctd_reset(&d);
	memset(name, 'N', sizeof(name) - 1);
	name[sizeof(name) - 1] = '\0';
	chk(dp_ctd_add(&d, name, (const uint8_t *)"zz", 2,
		       DP_EV_ALERT, DP_DIR_UNKNOWN) == 0, "long name accepted");
	chk(strlen(d.sig[0].name) == DP_CTD_NAME_MAX - 1,
	    "long name truncated and still NUL-terminated");

	dp_ctd_reset(&d);

	/* NULL must not crash anything. */
	chk(dp_ctd_scan(NULL, NULL) == DP_EV_NONE, "NULL scan safe");
	chk(dp_ctd_mem_use(NULL) == 0, "NULL mem_use safe");
	chk(dp_ctd_commit(NULL) == DP_CTD_BADARG, "NULL commit safe");
	dp_ctd_reset(NULL);
	dp_ctd_init(NULL, 0);
}

int main(void)
{
	printf("=== ffn_dp_ctd tests ===\n");
	test_verdicts();
	test_direction();
	test_streaming();
	test_truncation_breaks_stream();
	test_stale_state();
	test_config();
	test_limits();
	printf("=== %d passed, %d failed ===\n", pass, fail);
	return fail ? 1 : 0;
}
