// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * ffn_dp_ctd.c -- content inspection engine over the Aho-Corasick matcher.
 *
 * See ffn_dp_ctd.h for the config grammar and the streaming limits.
 *
 * THREADING
 * ---------
 * The compiled automaton is immutable and may be shared read-only by every
 * core. The struct AROUND it is not: per-signature hit counters are
 * incremented without locking, exactly as ffn_dp_dlp.c does. That is a
 * deliberate choice, not an oversight -- a lost increment on a statistics
 * counter is cheaper than a lock on the packet path, and no forwarding
 * decision reads these counters.
 */

#include <stdlib.h>
#include <string.h>

#include "ffn_dp_engine.h"
#include "ffn_dp_aho.h"
#include "ffn_dp_ctd.h"

/* dp_flow_ent stores the scanner position as uint16_t to fit the padding that
 * was already there. That is exact today -- a state index is at most
 * DP_AHO_MAX_STATES-1 -- and this stops it quietly becoming a truncation if
 * anyone raises the cap. */
#if DP_AHO_MAX_STATES > 65536u
#error "dp_engine_ctx.scan_state is uint16_t; raising DP_AHO_MAX_STATES past 65536 truncates it"
#endif

/* ---- build ------------------------------------------------------------ */

void dp_ctd_init(struct dp_ctd *d, int case_fold)
{
	if (!d)
		return;
	memset(d, 0, sizeof(*d));
	d->case_fold = case_fold ? 1 : 0;
}

void dp_ctd_reset(struct dp_ctd *d)
{
	int fold;

	if (!d)
		return;
	fold = d->case_fold;
	dp_aho_free(d->ac);
	memset(d, 0, sizeof(*d));
	d->case_fold = fold;
}

int dp_ctd_add(struct dp_ctd *d, const char *name,
	       const uint8_t *pat, uint32_t patlen,
	       int action, int direction)
{
	struct dp_ctd_sig *s;
	uint32_t id;
	int rc;

	if (!d || !name || !pat || patlen == 0)
		return DP_CTD_BADARG;
	if (d->built)
		return DP_CTD_LOCKED;
	if (d->count >= DP_CTD_SIG_MAX)
		return DP_CTD_FULL;
	if (action < DP_EV_ALERT || action > DP_EV_RESET)
		return DP_CTD_BADARG;
	if (direction < DP_DIR_UNKNOWN || direction > DP_DIR_EGRESS)
		return DP_CTD_BADARG;

	if (!d->ac) {
		d->ac = dp_aho_new(d->case_fold ? DP_AHO_CASE_FOLD
						: DP_AHO_CASE_SENSITIVE);
		if (!d->ac)
			return DP_CTD_NOMEM;
	}

	/* The signature index IS the pattern id, which is what makes the match
	 * callback a single array index rather than a search. */
	id = d->count;
	rc = dp_aho_add(d->ac, pat, patlen, id);
	if (rc != DP_AHO_OK)
		return (rc == DP_AHO_NOMEM || rc == DP_AHO_TOO_MANY)
		       ? DP_CTD_NOMEM : DP_CTD_BADARG;

	s = &d->sig[id];
	memset(s, 0, sizeof(*s));
	/* Truncate rather than refuse: a long name is a cosmetic problem in a
	 * log line, and refusing the signature over it would lose the
	 * detection. strncpy leaves no terminator at exactly NAME_MAX, so the
	 * last byte is cleared by the memset above and never written here. */
	strncpy(s->name, name, DP_CTD_NAME_MAX - 1);
	s->patlen    = patlen;
	s->action    = (uint8_t)action;
	s->direction = (uint8_t)direction;
	s->enabled   = 1;

	d->count++;
	return (int)id;
}

int dp_ctd_commit(struct dp_ctd *d)
{
	int rc;

	if (!d)
		return DP_CTD_BADARG;
	if (d->built)
		return DP_CTD_OK;               /* idempotent */
	if (d->count == 0 || !d->ac)
		return DP_CTD_EMPTY;

	rc = dp_aho_compile(d->ac);
	if (rc != DP_AHO_OK)
		return (rc == DP_AHO_NOMEM) ? DP_CTD_NOMEM : DP_CTD_BADARG;

	d->built = 1;
	return DP_CTD_OK;
}

uint64_t dp_ctd_mem_use(const struct dp_ctd *d)
{
	if (!d || !d->ac)
		return 0;
	return (uint64_t)dp_aho_mem_use(d->ac);
}

/* ---- scan ------------------------------------------------------------- */

struct ctd_cb {
	struct dp_ctd        *d;
	struct dp_engine_ctx *ec;
	int                   worst;
};

static int ctd_on_match(void *vctx, uint32_t id, uint32_t patlen, size_t at)
{
	struct ctd_cb *cb = (struct ctd_cb *)vctx;
	struct dp_ctd_sig *s;

	if (id >= cb->d->count)
		return 0;                       /* cannot happen; do not trust */

	s = &cb->d->sig[id];
	if (!s->enabled)
		return 0;

	/* Same direction rule as the DLP engine, and the same caveat: it filters
	 * only when BOTH sides are known, so until the port table carries a zone
	 * or trust attribute a direction-scoped signature fires both ways. */
	if (s->direction != DP_DIR_UNKNOWN &&
	    cb->ec->direction != DP_DIR_UNKNOWN &&
	    s->direction != cb->ec->direction)
		return 0;

	s->hits++;
	cb->d->stat_matches++;

	if (s->action > cb->worst) {
		cb->worst = s->action;
		cb->ec->hit_rule = s->name;
		/* `at` is the match's LAST byte. Report the first, which is what
		 * an operator reading a log wants -- except when the match began
		 * in an earlier packet, where there is no such offset in this
		 * buffer and 0 is the honest answer. */
		cb->ec->hit_offset = (at + 1 >= (size_t)patlen)
				     ? (uint32_t)(at + 1 - patlen) : 0u;
	}

	/* Nothing a later signature finds can make the packet more blocked. */
	return cb->worst >= DP_EV_BLOCK;
}

int dp_ctd_scan(struct dp_engine_ctx *ctx, void *state)
{
	struct dp_ctd *d = (struct dp_ctd *)state;
	struct ctd_cb cb;
	uint32_t st;
	int rc;

	if (!d || !d->built || !ctx || !ctx->payload || ctx->payload_len == 0)
		return DP_EV_NONE;

	cb.d     = d;
	cb.ec    = ctx;
	cb.worst = DP_EV_NONE;

	st = ctx->scan_state ? (uint32_t)*ctx->scan_state : DP_AHO_START_STATE;

	d->stat_scanned++;
	rc = dp_aho_scan(d->ac, &st, ctx->payload, ctx->payload_len,
			 ctd_on_match, &cb);

	/* A state index left on a flow by a PREVIOUS pattern set. The matcher
	 * rejects an out-of-range one rather than indexing past its table, so
	 * recover by restarting this flow at the root. An index that happens to
	 * be in range for the new automaton cannot be detected this way and will
	 * mean something different for the remainder of the flow's scan budget
	 * -- bounded, by construction, to DP_ENGINE_FLOW_PKTS packets. */
	if (rc < 0) {
		st = DP_AHO_START_STATE;
		cb.worst = DP_EV_NONE;
		rc = dp_aho_scan(d->ac, &st, ctx->payload, ctx->payload_len,
				 ctd_on_match, &cb);
		if (rc < 0)
			return DP_EV_NONE;
	}

	if (ctx->scan_state) {
		if (ctx->truncated) {
			/* The tail of this packet was never scanned, so the next
			 * packet does not continue the byte stream we were
			 * matching. Carrying the state over that gap would splice
			 * two non-adjacent byte ranges and report a signature
			 * that is not in the traffic. */
			*ctx->scan_state = (uint16_t)DP_AHO_START_STATE;
			d->stat_streams_broken++;
		} else {
			*ctx->scan_state = (uint16_t)st;
		}
	}

	return cb.worst;
}

/* ---- config ----------------------------------------------------------- */

static int hexval(int c)
{
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

/* Literal bytes with two escapes and no others. An unrecognised escape is
 * REFUSED, not copied through: a signature that silently means something other
 * than what the operator wrote is worse than one that fails to load. */
static int unescape(const char *in, uint8_t *out, uint32_t outmax,
		    uint32_t *outlen)
{
	uint32_t n = 0;

	while (*in) {
		unsigned char c = (unsigned char)*in++;

		if (c == '\\') {
			if (*in == '\\') {
				in++;
			} else if (*in == 'x' || *in == 'X') {
				int hi = hexval((unsigned char)in[1]);
				int lo = (hi < 0) ? -1
						  : hexval((unsigned char)in[2]);
				if (lo < 0)
					return DP_CTD_BADARG;
				c = (unsigned char)((hi << 4) | lo);
				in += 3;
			} else {
				return DP_CTD_BADARG;
			}
		}
		if (n >= outmax)
			return DP_CTD_BADARG;
		out[n++] = c;
	}
	*outlen = n;
	return DP_CTD_OK;
}

static int parse_action(const char *s, size_t n)
{
	if (n == 5 && !memcmp(s, "alert", 5)) return DP_EV_ALERT;
	if (n == 5 && !memcmp(s, "block", 5)) return DP_EV_BLOCK;
	if (n == 5 && !memcmp(s, "reset", 5)) return DP_EV_RESET;
	return -1;
}

static int parse_dir(const char *s, size_t n)
{
	if (n == 3 && !memcmp(s, "any",     3)) return DP_DIR_UNKNOWN;
	if (n == 7 && !memcmp(s, "ingress", 7)) return DP_DIR_INGRESS;
	if (n == 6 && !memcmp(s, "egress",  6)) return DP_DIR_EGRESS;
	return -1;
}

int dp_ctd_config_line(struct dp_ctd *d, const char *key, const char *val)
{
	static const char PFX_SIG[]  = "dp.ctd.sig.";
	static const char KEY_CASE[] = "dp.ctd.case";
	const char *name, *p, *q;
	uint8_t pat[DP_AHO_MAX_PATLEN];
	uint32_t patlen;
	int action, dir, rc;

	if (!d || !key || !val)
		return 0;

	if (!strcmp(key, KEY_CASE)) {
		/* Case mode is a property of the automaton, which is built from
		 * the first signature onwards, so changing it afterwards would
		 * fold some patterns and not others. */
		if (d->ac)
			return -1;
		if (!strcmp(val, "fold"))           d->case_fold = 1;
		else if (!strcmp(val, "sensitive")) d->case_fold = 0;
		else                                return -1;
		return 1;
	}

	if (strncmp(key, PFX_SIG, sizeof(PFX_SIG) - 1))
		return 0;                       /* not ours */

	name = key + sizeof(PFX_SIG) - 1;
	if (*name == '\0')
		return -1;

	/* <action>:<direction>:<pattern> -- split on the first TWO colons only.
	 * Patterns contain colons all the time ("http://", "Authorization:"),
	 * so splitting on every colon would corrupt most real signatures. */
	p = strchr(val, ':');
	if (!p)
		return -1;
	q = strchr(p + 1, ':');
	if (!q)
		return -1;

	action = parse_action(val, (size_t)(p - val));
	dir    = parse_dir(p + 1, (size_t)(q - (p + 1)));
	if (action < 0 || dir < 0)
		return -1;

	if (unescape(q + 1, pat, (uint32_t)sizeof(pat), &patlen) != DP_CTD_OK)
		return -1;
	if (patlen == 0)
		return -1;

	rc = dp_ctd_add(d, name, pat, patlen, action, dir);
	return (rc < 0) ? -1 : 1;
}
