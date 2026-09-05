/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_engine.c -- registry and dispatch for the inline analysis engines.
 *
 * Deliberately small. The dispatcher's only jobs are to run enabled engines in
 * a fixed order, combine their verdicts by severity, and stop early when the
 * answer can no longer change. Everything interesting lives in the engines.
 */
#include <string.h>

#include "ffn_dp_engine.h"
#include "ffn_dp_oct.h"          /* DP_ERR_* */

int dp_engine_register(struct dp_engine_set *set, const char *name,
		       int (*scan)(struct dp_engine_ctx *, void *), void *state)
{
	struct dp_engine *e;
	size_t n;

	if (!set || !name || !scan)
		return DP_ERR_RANGE;
	if (set->count >= DP_ENGINE_MAX)
		return DP_ERR_TOOMANY;

	n = strlen(name);
	if (n == 0 || n >= DP_ENGINE_NAME_MAX)
		return DP_ERR_RANGE;

	/* Re-registering a name replaces it rather than adding a duplicate: two
	 * engines answering to one name would make dp_engine_enable() ambiguous
	 * and the stats meaningless. */
	for (uint32_t i = 0; i < set->count; i++) {
		if (strcmp(set->e[i].name, name) == 0) {
			set->e[i].scan = scan;
			set->e[i].state = state;
			return (int)i;
		}
	}

	e = &set->e[set->count];
	memset(e, 0, sizeof(*e));
	memcpy(e->name, name, n + 1);
	e->scan = scan;
	e->state = state;
	/* Registered DISABLED. An engine that starts scanning the moment it is
	 * linked in would change forwarding behaviour as a side effect of a
	 * build change; enabling is always an explicit config act. */
	e->enabled = 0;
	return (int)set->count++;
}

int dp_engine_enable(struct dp_engine_set *set, const char *name, int on)
{
	if (!set || !name)
		return -1;
	for (uint32_t i = 0; i < set->count; i++) {
		if (strcmp(set->e[i].name, name) == 0) {
			set->e[i].enabled = on ? 1 : 0;
			return 0;
		}
	}
	return -1;
}

int dp_engine_scan(struct dp_engine_set *set, struct dp_engine_ctx *ctx)
{
	int worst = DP_EV_NONE;

	if (!set || !ctx)
		return DP_EV_NONE;

	set->stat_packets++;

	/* Nothing to analyse. Counted rather than silently returned: a high
	 * skip count against a high inspect count means traffic is being marked
	 * for inspection that the engines can never say anything about, which is
	 * a policy problem, not an engine one. */
	if (!ctx->payload || ctx->payload_len == 0) {
		set->stat_skipped++;
		return DP_EV_NONE;
	}

	/* Clip once, here, rather than trusting each engine to respect the
	 * budget. An engine that forgets cannot then run away. */
	if (ctx->payload_len > DP_ENGINE_SCAN_MAX)
		ctx->payload_len = DP_ENGINE_SCAN_MAX;

	for (uint32_t i = 0; i < set->count; i++) {
		struct dp_engine *e = &set->e[i];
		int v;

		if (!e->enabled)
			continue;

		e->stat_scanned++;
		v = e->scan(ctx, e->state);

		if (v == DP_EV_ALERT)
			e->stat_alert++;
		else if (v >= DP_EV_BLOCK)
			e->stat_block++;

		if (v > worst) {
			worst = v;
			/* hit_* were set by this engine; name it so a log line
			 * can say which engine decided, not just what. */
			ctx->hit_engine = e->name;
		}

		/* Stop at block or worse. A second opinion cannot make the
		 * packet more dropped, and continuing spends budget on a packet
		 * that is already going away. */
		if (worst >= DP_EV_BLOCK)
			break;
	}

	if (worst == DP_EV_NONE)
		set->stat_skipped += 0;   /* scanned, found nothing: not a skip */

	return worst;
}

int dp_engine_config_line(struct dp_engine_set *set, const char *key,
			  const char *val)
{
	const char *p, *dot;
	char name[DP_ENGINE_NAME_MAX];
	size_t n;

	if (!set || !key || !val)
		return 0;
	if (strncmp(key, "dp.engine.", 10) != 0)
		return 0;

	p = key + 10;
	dot = strrchr(p, '.');
	if (!dot || strcmp(dot, ".enable") != 0)
		return 0;

	n = (size_t)(dot - p);
	if (n == 0 || n >= sizeof(name))
		return 0;
	memcpy(name, p, n);
	name[n] = '\0';

	/* An unknown engine name is CONSUMED (returns 1) but not applied. The
	 * key is unmistakably ours, so reporting it as someone else's would send
	 * the caller looking in the wrong place; dp_engine_enable's -1 is what
	 * says it did not land. */
	(void)dp_engine_enable(set, name,
			       (val[0] == '1' || val[0] == 'y' || val[0] == 't'));
	return 1;
}
