/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_dlp.c -- the DLP scanner, the dataplane's first inline analysis engine.
 *
 * It consumes the rules the manager already holds (dlp_rules): a type, an
 * action, a direction and a pattern. The five shipped rules are Credit Card,
 * US SSN, AWS Access Key, Private Key Block and Confidential Marker -- so the
 * types that matter are credit_card, ssn, api_key and keyword.
 *
 * WHY STRUCTURAL MATCHING AND NOT REGEX
 * -------------------------------------
 * A credit card is not "16 digits". Sixteen digits appear in timestamps, IDs,
 * hashes and base64 by the thousand, and a rule that blocked all of them would
 * be turned off within a day -- which is worse than no rule. So the numeric
 * types are matched structurally and then VALIDATED: a card candidate must pass
 * Luhn, an SSN must not use an area number the SSA never issued. That check is
 * what makes the difference between a control and a nuisance.
 *
 * Regex is deliberately absent. A backtracking engine on a forwarding path is a
 * denial of service against the forwarder, and a non-backtracking one is a
 * compiler this file has no business containing. Anything richer than these
 * types should be compiled to a bounded automaton before it reaches here.
 *
 * ALL SCANNING IS BYTE ORIENTED. The OCTEON is big-endian MIPS64 and packet
 * bytes are read one at a time, so there is no multi-byte load to get wrong --
 * which is why the tests are expected to pass identically under qemu-mips64.
 */
#include <string.h>

#include "ffn_dp_dlp.h"
#include "ffn_dp_engine.h"

/* ---------------------------------------------------------------------------
 * validators
 * ------------------------------------------------------------------------- */

/* Luhn, over a run of ASCII digits. This is the whole reason the credit_card
 * type is usable: it rejects ~90% of random digit runs of the same length. */
static int luhn_ok(const uint8_t *d, uint32_t n)
{
	uint32_t sum = 0;
	int dbl = 0;

	if (n < 13 || n > 19)
		return 0;

	/* Right to left: double every second digit, casting out nines. */
	for (uint32_t i = n; i > 0; i--) {
		int v = d[i - 1] - '0';

		if (dbl) {
			v <<= 1;
			if (v > 9)
				v -= 9;
		}
		sum += (uint32_t)v;
		dbl = !dbl;
	}
	return (sum % 10) == 0;
}

/* A US SSN is NNN-NN-NNNN, but not every such string is one. The SSA has never
 * issued area 000, 666, or 900-999, nor group 00, nor serial 0000. Excluding
 * them removes the most common false positives -- phone numbers, part numbers
 * and zero-padded identifiers -- at no cost to real detection. */
static int ssn_valid(const uint8_t *p)
{
	int area  = (p[0]-'0')*100 + (p[1]-'0')*10 + (p[2]-'0');
	int group = (p[4]-'0')*10  + (p[5]-'0');
	int ser   = (p[7]-'0')*1000 + (p[8]-'0')*100 + (p[9]-'0')*10 + (p[10]-'0');

	if (area == 0 || area == 666 || area >= 900)
		return 0;
	if (group == 0 || ser == 0)
		return 0;
	return 1;
}

static int is_digit(uint8_t c) { return c >= '0' && c <= '9'; }
static int is_upper_alnum(uint8_t c)
{
	return (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
}

/* ---------------------------------------------------------------------------
 * matchers -- each returns the offset of a hit, or -1
 * ------------------------------------------------------------------------- */

static long find_credit_card(const uint8_t *b, uint32_t n)
{
	uint32_t i = 0;

	while (i < n) {
		uint32_t start, digits = 0;
		uint8_t  compact[19];

		if (!is_digit(b[i])) { i++; continue; }

		/* Collect a run, tolerating the separators humans and CSVs use.
		 * Without this, "4111 1111 1111 1111" -- the most likely way a
		 * card actually appears in traffic -- would never be seen. */
		start = i;
		while (i < n && digits < sizeof(compact)) {
			if (is_digit(b[i])) {
				compact[digits++] = b[i];
				i++;
			} else if ((b[i] == ' ' || b[i] == '-') && digits > 0 &&
				   i + 1 < n && is_digit(b[i + 1])) {
				i++;                  /* separator inside a run */
			} else {
				break;
			}
		}
		/* Do not accept a candidate that is merely the head of a longer
		 * digit run: a 20-digit id would otherwise match on its first 19. */
		if (i < n && is_digit(b[i])) {
			while (i < n && (is_digit(b[i]) || b[i] == ' ' || b[i] == '-'))
				i++;
			continue;
		}
		if (digits >= 13 && luhn_ok(compact, digits))
			return (long)start;
	}
	return -1;
}

static long find_ssn(const uint8_t *b, uint32_t n)
{
	if (n < 11)
		return -1;
	for (uint32_t i = 0; i + 11 <= n; i++) {
		if (is_digit(b[i]) && is_digit(b[i+1]) && is_digit(b[i+2]) &&
		    b[i+3] == '-' && is_digit(b[i+4]) && is_digit(b[i+5]) &&
		    b[i+6] == '-' && is_digit(b[i+7]) && is_digit(b[i+8]) &&
		    is_digit(b[i+9]) && is_digit(b[i+10])) {
			/* Reject when embedded in a longer digit run, which is
			 * how dates and serials produce false hits. */
			if (i > 0 && is_digit(b[i-1]))
				continue;
			if (i + 11 < n && is_digit(b[i+11]))
				continue;
			if (ssn_valid(b + i))
				return (long)i;
		}
	}
	return -1;
}

/* AWS-style access key id: a 4-char prefix then 16 uppercase alphanumerics.
 * The prefix set is explicit rather than "any 4 uppercase" because AKIA/ASIA/
 * AIDA/AROA are what AWS actually issues, and a looser rule matches ordinary
 * uppercase text. */
static const char *const AWS_PREFIX[] = { "AKIA", "ASIA", "AIDA", "AROA", NULL };

static long find_api_key(const uint8_t *b, uint32_t n)
{
	if (n < 20)
		return -1;
	for (uint32_t i = 0; i + 20 <= n; i++) {
		int matched = 0;

		for (int p = 0; AWS_PREFIX[p]; p++) {
			if (memcmp(b + i, AWS_PREFIX[p], 4) == 0) { matched = 1; break; }
		}
		if (!matched)
			continue;
		if (i > 0 && is_upper_alnum(b[i-1]))
			continue;              /* part of a longer token */

		uint32_t k = 4;
		while (k < 20 && is_upper_alnum(b[i + k]))
			k++;
		if (k != 20)
			continue;
		/* Check the TRAILING boundary too. Without this,
		 * "AKIAIOSFODNN7EXAMPLEEXTRA" matches on its first 20
		 * characters -- a substring of a longer token is not an access
		 * key, and treating it as one is how a credential scanner
		 * starts alerting on base64 and gets turned off. */
		if (i + 20 < n && is_upper_alnum(b[i + 20]))
			continue;
		return (long)i;
	}
	return -1;
}

static long find_keyword(const uint8_t *b, uint32_t n,
			 const char *pat, uint32_t plen)
{
	if (plen == 0 || plen > n)
		return -1;
	/* Plain search. For the handful of keyword rules a policy carries this
	 * beats an automaton: no build step, no state, and the constant factor
	 * dominates at these sizes. If keyword counts ever reach the dozens,
	 * replace this with Aho-Corasick -- not with a regex. */
	for (uint32_t i = 0; i + plen <= n; i++) {
		if (b[i] == (uint8_t)pat[0] && memcmp(b + i, pat, plen) == 0)
			return (long)i;
	}
	return -1;
}

/* ---------------------------------------------------------------------------
 * engine
 * ------------------------------------------------------------------------- */

int dp_dlp_scan(struct dp_engine_ctx *ctx, void *state)
{
	struct dp_dlp *d = (struct dp_dlp *)state;
	int worst = DP_EV_NONE;

	if (!d || !ctx || !ctx->payload || ctx->payload_len == 0)
		return DP_EV_NONE;

	for (uint32_t i = 0; i < d->count; i++) {
		struct dp_dlp_rule *r = &d->rule[i];
		long off = -1;

		if (!r->enabled)
			continue;

		/* Direction is a first-class filter, not a refinement. These
		 * rules exist to stop data LEAVING; running them on ingress
		 * would alert on every inbound form post and teach operators to
		 * ignore the engine. */
		if (r->direction != DP_DIR_UNKNOWN &&
		    ctx->direction != DP_DIR_UNKNOWN &&
		    r->direction != ctx->direction)
			continue;

		switch (r->type) {
		case DP_DLP_CREDIT_CARD:
			off = find_credit_card(ctx->payload, ctx->payload_len);
			break;
		case DP_DLP_SSN:
			off = find_ssn(ctx->payload, ctx->payload_len);
			break;
		case DP_DLP_API_KEY:
			off = find_api_key(ctx->payload, ctx->payload_len);
			break;
		case DP_DLP_KEYWORD:
			off = find_keyword(ctx->payload, ctx->payload_len,
					   r->pattern, r->pattern_len);
			break;
		default:
			break;
		}

		if (off < 0)
			continue;

		r->hits++;
		if (r->action > worst) {
			worst = r->action;
			ctx->hit_rule = r->name;
			ctx->hit_offset = (uint32_t)off;
		}
		/* A block ends the scan: nothing a later rule finds can make the
		 * packet more blocked, and the first hit is the one to report. */
		if (worst >= DP_EV_BLOCK)
			break;
	}
	return worst;
}

/* ---------------------------------------------------------------------------
 * config:  dp.dlp.rule.<id> = <type>:<action>:<direction>:<pattern>
 * e.g.     dp.dlp.rule.4 = keyword:block:egress:-----BEGIN PRIVATE KEY-----
 *
 * The pattern is last and unescaped on purpose: it is taken verbatim to the end
 * of the value, so a pattern containing ':' needs no quoting. Quoting rules are
 * where a config format acquires its first security bug.
 * ------------------------------------------------------------------------- */

static int parse_type(const char *s, uint32_t n)
{
	if (n == 11 && memcmp(s, "credit_card", 11) == 0) return DP_DLP_CREDIT_CARD;
	if (n == 3  && memcmp(s, "ssn", 3) == 0)          return DP_DLP_SSN;
	if (n == 7  && memcmp(s, "api_key", 7) == 0)      return DP_DLP_API_KEY;
	if (n == 7  && memcmp(s, "keyword", 7) == 0)      return DP_DLP_KEYWORD;
	return -1;
}

static int parse_action(const char *s, uint32_t n)
{
	if (n == 5 && memcmp(s, "alert", 5) == 0) return DP_EV_ALERT;
	if (n == 5 && memcmp(s, "block", 5) == 0) return DP_EV_BLOCK;
	if (n == 5 && memcmp(s, "reset", 5) == 0) return DP_EV_RESET;
	return -1;
}

static int parse_dir(const char *s, uint32_t n)
{
	if (n == 6 && memcmp(s, "egress", 6) == 0)  return DP_DIR_EGRESS;
	if (n == 7 && memcmp(s, "ingress", 7) == 0) return DP_DIR_INGRESS;
	if (n == 3 && memcmp(s, "any", 3) == 0)     return DP_DIR_UNKNOWN;
	return -1;
}

int dp_dlp_config_line(struct dp_dlp *d, const char *key, const char *val)
{
	const char *f[3], *p;
	uint32_t flen[3];
	struct dp_dlp_rule *r;
	int type, action, dir;
	size_t plen;

	if (!d || !key || !val)
		return 0;
	if (strncmp(key, "dp.dlp.rule.", 12) != 0)
		return 0;

	/* Split the first three ':' fields; the rest is the pattern. */
	p = val;
	for (int i = 0; i < 3; i++) {
		const char *c = strchr(p, ':');
		if (!c)
			return -1;              /* malformed: consumed, rejected */
		f[i] = p;
		flen[i] = (uint32_t)(c - p);
		p = c + 1;
	}

	type   = parse_type(f[0], flen[0]);
	action = parse_action(f[1], flen[1]);
	dir    = parse_dir(f[2], flen[2]);
	if (type < 0 || action < 0 || dir < 0)
		return -1;

	plen = strlen(p);
	if (type == DP_DLP_KEYWORD && (plen == 0 || plen >= DP_DLP_PAT_MAX))
		return -1;                      /* a keyword rule needs a keyword */
	if (d->count >= DP_DLP_RULE_MAX)
		return -1;

	r = &d->rule[d->count];
	memset(r, 0, sizeof(*r));
	r->type = (uint8_t)type;
	r->action = (uint8_t)action;
	r->direction = (uint8_t)dir;
	r->enabled = 1;
	if (plen && plen < DP_DLP_PAT_MAX) {
		memcpy(r->pattern, p, plen + 1);
		r->pattern_len = (uint32_t)plen;
	}
	/* The rule id from the key, so a log line can be traced back to config. */
	{
		const char *id = key + 12;
		size_t n = strlen(id);
		if (n >= sizeof(r->name))
			n = sizeof(r->name) - 1;
		memcpy(r->name, id, n);
		r->name[n] = '\0';
	}
	d->count++;
	return 1;
}
