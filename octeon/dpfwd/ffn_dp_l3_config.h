/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_config.h -- populate the FIB from the config the MP relays down.
 *
 * Closes the last link in the chain:
 *   MP config.env -> ffn_cfgd -> ffn_cfgagent (CP) -> PCIe mailbox -> DP dp.env
 *   -> here -> dp_l3_route_add() / dp_l3_neigh_add()
 *
 * Syntax (iproute2-shaped, so it reads the way an operator expects):
 *   dp.l3.route.<id>=<prefix>/<len> [via <gateway>] dev <egress>
 *   dp.l3.route.<id>=<prefix>/<len> nexthop via <gw> dev <n> nexthop ...
 *   dp.l3.neigh.<id>=<ip> lladdr <aa:bb:cc:dd:ee:ff>
 *   dp.l3.iface.<egress>.mac=<aa:bb:cc:dd:ee:ff>
 *
 * A route with no "via" is directly connected. <id> exists only so distinct
 * routes are distinct keys in a flat namespace; nothing depends on its value.
 *
 * The multipath form builds an ECMP group; see cfg_route_multipath(). The
 * single-path form is NOT silently promoted to a one-member group, because
 * that is a different object in the FIB.
 *
 * IPv6 is the same shape in ffn_dp_l3_v6_config.h, kept in a separate object
 * so a v4-only build does not have to link the v6 tables.
 */
#ifndef FFN_DP_L3_CONFIG_H
#define FFN_DP_L3_CONFIG_H

#include "ffn_dp_l3.h"

/* A multipath value is long: a prefix plus up to DP_L3_ECMP_MAX legs of
 * "nexthop via <addr> dev <n>" -- which is FIVE tokens per leg, not four.
 *
 * Room for ONE LEG MORE THAN IS LEGAL, deliberately. Sizing this to the
 * largest valid input makes "too many legs" tokenise into exactly the maximum
 * and be accepted, silently dropping the legs that did not fit -- a route that
 * forwards over fewer paths than the operator configured, with nothing
 * reporting it. With room to spare, an over-long line is still visibly
 * over-long and cfg_route_multipath() rejects it.
 *
 * Getting this wrong is not hypothetical: at 1 + 4*8 the truncation landed
 * mid-leg and every route with more than six legs was rejected as malformed,
 * while DP_L3_ECMP_MAX said eight were allowed. */
#define DP_L3_CFG_MAX_TOK    (1 + 5 * ((int)DP_L3_ECMP_MAX + 1))
#define DP_L3_CFG_MAX_VALUE  512
#define DP_L3_CFG_MAX_LINE   1024

#define DP_L3_CFG_OK          0
#define DP_L3_CFG_SKIP        1    /* not an l3 key -- normal, not an error */
#define DP_L3_CFG_ERR_OPEN  (-30)
#define DP_L3_CFG_ERR_SYNTAX (-31)
#define DP_L3_CFG_ERR_ADDR  (-32)
#define DP_L3_CFG_ERR_APPLY (-33)

struct dp_l3_config_stats {
    uint32_t routes, neigh, ifaces, ignored, errors;
};

/* Apply one key/value pair. Returns DP_L3_CFG_SKIP for keys that are not ours.
 * Errors are counted in st and reported, never fatal. */
int dp_l3_config_line(struct dp_l3 *l3, const char *key, const char *value,
                      struct dp_l3_config_stats *st);

/* Apply a whole relayed config file. Unknown keys are counted and skipped. */
int dp_l3_config_apply(struct dp_l3 *l3, const char *path,
                       struct dp_l3_config_stats *st);

#endif /* FFN_DP_L3_CONFIG_H */
