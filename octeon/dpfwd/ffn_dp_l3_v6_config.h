/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_v6_config.h -- populate the IPv6 FIB from the relayed config.
 *
 * The v6 half of ffn_dp_l3_config.h, and it exists for the same reason that
 * one did: dp_l3_route6_add() had no caller outside the unit tests, so the
 * relay carried v6 keys down to the dataplane and dropped them. An IPv6 FIB
 * nothing can configure is an IPv6 FIB nobody has.
 *
 * Syntax, identical in shape to v4 so one set of habits covers both:
 *
 *   dp.l3.route6.<id>=<prefix>/<len> [via <gateway>] dev <egress>
 *   dp.l3.route6.<id>=<prefix>/<len> nexthop via <gw> dev <n> nexthop ...
 *   dp.l3.neigh6.<id>=<ip> lladdr <aa:bb:cc:dd:ee:ff>
 *   dp.l3.iface6.<egress>.mac=<aa:bb:cc:dd:ee:ff>
 *   dp.l3.iface6.<egress>.ip=<ip>
 *
 * SEPARATE OBJECT, ON PURPOSE. The v4 config layer is in L3_SRC and the v6
 * tables are in V6_SRC; folding v6 key handling into ffn_dp_l3_config.c would
 * drag ffn_dp_l3_v6.o into every v4-only link, including the l3-test binary
 * that exists precisely to exercise v4 on its own.
 *
 * The key namespaces cannot collide: "dp.l3.route." and "dp.l3.route6." differ
 * at the twelfth character, so the v4 matcher never sees a v6 key and vice
 * versa. Each layer counts the other's keys as ignored, which is why the two
 * apply calls take their own stats.
 */
#ifndef FFN_DP_L3_V6_CONFIG_H
#define FFN_DP_L3_V6_CONFIG_H

#include "ffn_dp_l3_v6.h"
#include "ffn_dp_l3_config.h"   /* DP_L3_CFG_* and struct dp_l3_config_stats */

/* Apply one key/value pair. DP_L3_CFG_SKIP for keys that are not ours. */
int dp_l3_v6_config_line(struct dp_l3_v6 *v6, const char *key,
                         const char *value, struct dp_l3_config_stats *st);

/* Apply a whole relayed config file. Unknown keys are counted and skipped. */
int dp_l3_v6_config_apply(struct dp_l3_v6 *v6, const char *path,
                          struct dp_l3_config_stats *st);

#endif /* FFN_DP_L3_V6_CONFIG_H */
