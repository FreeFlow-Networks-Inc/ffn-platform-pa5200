/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_engine_test.c -- unit tests for the inline analysis engines.
 *
 * Runs natively AND under qemu-mips64 (big-endian), like the other dataplane
 * tests. Here the point is slightly different from the L3 tests: the scanners
 * are byte-oriented by construction, so a passing native run and a failing
 * big-endian one would mean a multi-byte read crept in somewhere. Running both
 * is how that stays true rather than merely intended.
 *
 * The tests lean hard on FALSE POSITIVES, because that is what decides whether
 * a DLP engine survives contact with real traffic. A scanner that flags every
 * 16-digit number gets switched off in a day, and a switched-off control
 * protects nothing -- so "this must NOT match" is tested at least as heavily as
 * "this must".
 */
#include <stdio.h>
#include <string.h>

#include "ffn_dp_engine.h"
#include "ffn_dp_dlp.h"

static int fails;

static void check(int cond, const char *what)
{
	if (!cond) {
		printf("  FAIL: %s\n", what);
		fails++;
	}
}

/* Scan one payload through the DLP engine directly. */
static int scan(struct dp_dlp *d, const char *s, int dir, const char **rule)
{
	struct dp_engine_ctx ctx;
	int v;

	memset(&ctx, 0, sizeof(ctx));
	ctx.payload = (const uint8_t *)s;
	ctx.payload_len = (uint32_t)strlen(s);
	ctx.direction = (uint8_t)dir;
	v = dp_dlp_scan(&ctx, d);
	if (rule)
		*rule = ctx.hit_rule;
	return v;
}

static void test_credit_card(void)
{
	struct dp_dlp d;
	memset(&d, 0, sizeof(d));
	check(dp_dlp_config_line(&d, "dp.dlp.rule.1",
				 "credit_card:block:egress:") == 1, "cc rule parses");

	/* Real test numbers -- all Luhn-valid. */
	check(scan(&d, "pay 4111111111111111 now", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "visa 16 detected");
	check(scan(&d, "5500005555555559", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "mastercard detected");
	check(scan(&d, "378282246310005", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "amex 15 detected");
	/* The way a card actually appears in traffic. */
	check(scan(&d, "card: 4111 1111 1111 1111", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "space separated detected");
	check(scan(&d, "4111-1111-1111-1111", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "dash separated detected");

	/* FALSE POSITIVES -- the part that decides whether this is usable. */
	check(scan(&d, "4111111111111112", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "Luhn-invalid 16-digit run must NOT match");
	check(scan(&d, "id 1234567890123456 end", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "arbitrary 16-digit id must NOT match");
	check(scan(&d, "ts 20260904231500123456789", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "25-digit run must NOT match on its first 19");
	check(scan(&d, "123456789012", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "12 digits is too short to be a card");
}

static void test_ssn(void)
{
	struct dp_dlp d;
	memset(&d, 0, sizeof(d));
	check(dp_dlp_config_line(&d, "dp.dlp.rule.2",
				 "ssn:block:egress:") == 1, "ssn rule parses");

	check(scan(&d, "ssn 123-45-6789", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "valid SSN detected");

	/* Area/group/serial values the SSA never issued. */
	check(scan(&d, "000-45-6789", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "area 000 must NOT match");
	check(scan(&d, "666-45-6789", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "area 666 must NOT match");
	check(scan(&d, "900-45-6789", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "area 900+ must NOT match");
	check(scan(&d, "123-00-6789", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "group 00 must NOT match");
	check(scan(&d, "123-45-0000", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "serial 0000 must NOT match");
	/* Embedded in a longer digit run: a date range or serial, not an SSN. */
	check(scan(&d, "9123-45-67890", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "digits either side must NOT match");
}

static void test_api_key(void)
{
	struct dp_dlp d;
	memset(&d, 0, sizeof(d));
	check(dp_dlp_config_line(&d, "dp.dlp.rule.3",
				 "api_key:block:egress:") == 1, "api rule parses");

	check(scan(&d, "AKIAIOSFODNN7EXAMPLE", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "AKIA key detected");
	check(scan(&d, "key=ASIAIOSFODNN7EXAMPLE&x=1", DP_DIR_EGRESS, NULL) == DP_EV_BLOCK,
	      "ASIA key in a query string detected");

	check(scan(&d, "AKIASHORT", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "too short must NOT match");
	check(scan(&d, "NOTAKIAIOSFODNN7EXAMPLE", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "prefix inside a longer token must NOT match");
	check(scan(&d, "AKIAIOSFODNN7EXAMPLEEXTRA", DP_DIR_EGRESS, NULL) == DP_EV_NONE,
	      "longer token must NOT match");
}

static void test_keyword_and_direction(void)
{
	struct dp_dlp d;
	const char *rule = NULL;
	memset(&d, 0, sizeof(d));

	/* The shipped Private Key rule -- note the pattern contains no ':' but
	 * the next one does, which is the case the parser must not split on. */
	check(dp_dlp_config_line(&d, "dp.dlp.rule.4",
		"keyword:block:egress:-----BEGIN PRIVATE KEY-----") == 1,
	      "private key rule parses");
	check(dp_dlp_config_line(&d, "dp.dlp.rule.5",
		"keyword:alert:egress:CONFIDENTIAL") == 1,
	      "confidential rule parses");

	check(scan(&d, "x\n-----BEGIN PRIVATE KEY-----\nMII", DP_DIR_EGRESS, &rule)
	      == DP_EV_BLOCK, "private key block detected");
	check(rule && strcmp(rule, "4") == 0, "hit reports the rule id");

	check(scan(&d, "marked CONFIDENTIAL do not share", DP_DIR_EGRESS, NULL)
	      == DP_EV_ALERT, "confidential alerts but does not block");

	/* Direction: these rules stop data LEAVING. Running them inbound would
	 * alert on every form post and train operators to ignore the engine. */
	check(scan(&d, "marked CONFIDENTIAL", DP_DIR_INGRESS, NULL) == DP_EV_NONE,
	      "egress rule must NOT fire on ingress");

	/* Severity: block outranks alert even when alert matched first. */
	check(scan(&d, "CONFIDENTIAL -----BEGIN PRIVATE KEY-----", DP_DIR_EGRESS, NULL)
	      == DP_EV_BLOCK, "block outranks alert");
}

static void test_config_parsing(void)
{
	struct dp_dlp d;
	memset(&d, 0, sizeof(d));

	/* A pattern containing ':' must survive -- the value is split on the
	 * first three colons only, and the rest is taken verbatim. */
	check(dp_dlp_config_line(&d, "dp.dlp.rule.9",
		"keyword:block:any:Authorization: Bearer") == 1,
	      "pattern containing ':' parses");
	check(scan(&d, "GET / HTTP/1.1\r\nAuthorization: Bearer abc", DP_DIR_INGRESS, NULL)
	      == DP_EV_BLOCK, "colon-bearing pattern matches");

	check(dp_dlp_config_line(&d, "dp.other.key", "x") == 0, "foreign key ignored");
	check(dp_dlp_config_line(&d, "dp.dlp.rule.10", "nosuch:block:egress:x") == -1,
	      "unknown type rejected");
	check(dp_dlp_config_line(&d, "dp.dlp.rule.11", "keyword:nosuch:egress:x") == -1,
	      "unknown action rejected");
	check(dp_dlp_config_line(&d, "dp.dlp.rule.12", "keyword:block:egress:") == -1,
	      "empty keyword rejected");
	check(dp_dlp_config_line(&d, "dp.dlp.rule.13", "keyword:block") == -1,
	      "truncated value rejected");
}

static void test_registry(void)
{
	struct dp_engine_set set;
	struct dp_dlp d;
	struct dp_engine_ctx ctx;

	memset(&set, 0, sizeof(set));
	memset(&d, 0, sizeof(d));
	dp_dlp_config_line(&d, "dp.dlp.rule.1", "keyword:block:any:SECRETDATA");

	int idx = dp_engine_register(&set, "dlp_scanner", dp_dlp_scan, &d);
	check(idx == 0, "engine registers");
	check(set.count == 1, "one engine registered");

	/* Registered DISABLED: linking an engine in must not change forwarding. */
	check(set.e[0].enabled == 0, "engines register disabled");

	memset(&ctx, 0, sizeof(ctx));
	ctx.payload = (const uint8_t *)"has SECRETDATA inside";
	ctx.payload_len = 21;
	check(dp_engine_scan(&set, &ctx) == DP_EV_NONE,
	      "a disabled engine does not scan");

	check(dp_engine_config_line(&set, "dp.engine.dlp_scanner.enable", "1") == 1,
	      "enable key consumed");
	check(set.e[0].enabled == 1, "engine enabled by config");
	check(dp_engine_config_line(&set, "dp.engine.nosuch.enable", "1") == 1,
	      "unknown engine key still consumed (it is unmistakably ours)");
	check(dp_engine_config_line(&set, "cp.something", "1") == 0,
	      "foreign key not consumed");

	memset(&ctx, 0, sizeof(ctx));
	ctx.payload = (const uint8_t *)"has SECRETDATA inside";
	ctx.payload_len = 21;
	check(dp_engine_scan(&set, &ctx) == DP_EV_BLOCK, "enabled engine scans");
	check(ctx.hit_engine && strcmp(ctx.hit_engine, "dlp_scanner") == 0,
	      "hit names the engine");
	check(set.e[0].stat_block == 1, "block stat counted");

	/* No payload: counted as skipped, not silently ignored. */
	memset(&ctx, 0, sizeof(ctx));
	check(dp_engine_scan(&set, &ctx) == DP_EV_NONE, "no payload -> no verdict");
	check(set.stat_skipped == 1, "empty payload counted as skipped");

	/* Re-registering a name replaces rather than duplicating. */
	check(dp_engine_register(&set, "dlp_scanner", dp_dlp_scan, &d) == 0,
	      "re-register returns the same slot");
	check(set.count == 1, "re-register does not duplicate");
}

static void test_scan_budget(void)
{
	struct dp_engine_set set;
	struct dp_dlp d;
	struct dp_engine_ctx ctx;
	static uint8_t big[DP_ENGINE_SCAN_MAX * 2];

	memset(&set, 0, sizeof(set));
	memset(&d, 0, sizeof(d));
	dp_dlp_config_line(&d, "dp.dlp.rule.1", "keyword:block:any:NEEDLE");
	dp_engine_register(&set, "dlp_scanner", dp_dlp_scan, &d);
	dp_engine_enable(&set, "dlp_scanner", 1);

	memset(big, 'x', sizeof(big));
	/* Place the needle beyond the budget: it must NOT be found, and the
	 * budget must be enforced by the dispatcher rather than by each engine
	 * remembering to. */
	memcpy(big + DP_ENGINE_SCAN_MAX + 100, "NEEDLE", 6);

	memset(&ctx, 0, sizeof(ctx));
	ctx.payload = big;
	ctx.payload_len = sizeof(big);
	check(dp_engine_scan(&set, &ctx) == DP_EV_NONE,
	      "content beyond the scan budget is not matched");
	check(ctx.payload_len == DP_ENGINE_SCAN_MAX,
	      "dispatcher clipped payload_len to the budget");

	/* And inside the budget it still works. */
	memcpy(big + 10, "NEEDLE", 6);
	memset(&ctx, 0, sizeof(ctx));
	ctx.payload = big;
	ctx.payload_len = sizeof(big);
	check(dp_engine_scan(&set, &ctx) == DP_EV_BLOCK,
	      "content inside the budget is matched");
}

int main(void)
{
	printf("ffn_dp_engine_test: inline analysis engines\n");
	test_credit_card();
	test_ssn();
	test_api_key();
	test_keyword_and_direction();
	test_config_parsing();
	test_registry();
	test_scan_budget();

	if (fails) {
		printf("FAILED (%d)\n", fails);
		return 1;
	}
	printf("ok: all engine tests passed\n");
	return 0;
}
