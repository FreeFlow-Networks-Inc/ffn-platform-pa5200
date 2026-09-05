/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_dlp.h -- DLP scanner state, mirroring the manager's dlp_rules table.
 *
 * The field set is deliberately the same shape as the SQL table (type, action,
 * direction, pattern, enabled) so a rule can be rendered into config and back
 * without a translation layer inventing semantics on the way.
 */
#ifndef FFN_DP_DLP_H
#define FFN_DP_DLP_H

#include <stdint.h>

struct dp_engine_ctx;

/* Rule storage is fixed and inline: the dataplane does not allocate, and a
 * policy that outgrows this should fail loudly at config time rather than
 * quietly start dropping rules on the packet path. */
#define DP_DLP_RULE_MAX 32
#define DP_DLP_PAT_MAX  64
#define DP_DLP_NAME_MAX 24

enum dp_dlp_type {
	DP_DLP_KEYWORD     = 0,   /* literal substring                        */
	DP_DLP_CREDIT_CARD = 1,   /* digit run, Luhn-validated                */
	DP_DLP_SSN         = 2,   /* NNN-NN-NNNN, SSA-plausible               */
	DP_DLP_API_KEY     = 3    /* AWS-style access key id                  */
};

struct dp_dlp_rule {
	char     name[DP_DLP_NAME_MAX];   /* rule id, for logs                */
	char     pattern[DP_DLP_PAT_MAX]; /* keyword rules only               */
	uint32_t pattern_len;
	uint8_t  type;                    /* enum dp_dlp_type                 */
	uint8_t  action;                  /* enum dp_engine_verdict           */
	uint8_t  direction;               /* enum dp_direction                */
	uint8_t  enabled;
	uint64_t hits;
};

struct dp_dlp {
	struct dp_dlp_rule rule[DP_DLP_RULE_MAX];
	uint32_t           count;
};

/* Engine entry point, registered with dp_engine_register(). */
int dp_dlp_scan(struct dp_engine_ctx *ctx, void *state);

/* dp.dlp.rule.<id> = <type>:<action>:<direction>:<pattern>
 * Returns 1 consumed, 0 not ours, -1 consumed but malformed. */
int dp_dlp_config_line(struct dp_dlp *d, const char *key, const char *val);

#endif /* FFN_DP_DLP_H */
