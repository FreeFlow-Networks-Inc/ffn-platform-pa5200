/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_v6_config.c -- populate the IPv6 FIB from the relayed config.
 *
 * See ffn_dp_l3_v6_config.h for the syntax and for why this is its own object.
 *
 * Addresses land as WIRE BYTES, which is what struct dp_l3_v6 stores. There is
 * no byte-order decision to get wrong here: a 128-bit address has no
 * meaningful host order, so unlike the v4 path there is nothing to swap.
 */
#include <stdio.h>
#include <string.h>

#include "ffn_dp_l3_v6_config.h"
#include "ffn_dp_l3_parse.h"

/* ---- multipath --------------------------------------------------------- */

/* "<prefix> nexthop via <gw> dev <n> nexthop via <gw> dev <n> ..."
 *
 * Same contract as the v4 version: every leg needs "dev", "via" may be omitted
 * for a directly connected member, and the group is only created once all the
 * legs have parsed -- so a typo in the third leg cannot leave a two-member
 * group in the FIB that the operator never asked for.
 */
static int cfg6_route_multipath(struct dp_l3_v6 *v6, const uint8_t prefix[16],
                                uint8_t plen, char **tok, int n,
                                struct dp_l3_config_stats *st)
{
    uint8_t nh[DP_L3_ECMP_MAX][16];
    uint16_t eg[DP_L3_ECMP_MAX];
    int legs = 0, i = 1, group;

    memset(nh, 0, sizeof(nh));

    while (i < n) {
        int have_dev = 0;

        if (strcmp(tok[i], "nexthop") != 0) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        i++;
        if (legs >= (int)DP_L3_ECMP_MAX) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        eg[legs] = 0;

        while (i + 1 < n && strcmp(tok[i], "nexthop") != 0) {
            if (strcmp(tok[i], "via") == 0) {
                if (dp_l3_parse_ipv6(tok[i + 1], nh[legs]) != 0) {
                    st->errors++; return DP_L3_CFG_ERR_ADDR;
                }
            } else if (strcmp(tok[i], "dev") == 0) {
                if (dp_l3_parse_dev(tok[i + 1], &eg[legs]) != 0) {
                    st->errors++; return DP_L3_CFG_ERR_SYNTAX;
                }
                have_dev = 1;
            } else {
                st->errors++; return DP_L3_CFG_ERR_SYNTAX;
            }
            i += 2;
        }
        if (!have_dev) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        legs++;
    }
    if (legs < 1) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }

    group = dp_l3_v6_ecmp_add(v6, (const uint8_t (*)[16])nh, eg, (uint8_t)legs);
    if (group < 0) { st->errors++; return DP_L3_CFG_ERR_APPLY; }
    if (dp_l3_route6_add_ecmp(v6, prefix, plen, (uint16_t)group) != DP_L3_OK) {
        st->errors++; return DP_L3_CFG_ERR_APPLY;
    }
    st->routes++;
    return DP_L3_CFG_OK;
}

/* ---- one key = one object --------------------------------------------- */

int dp_l3_v6_config_line(struct dp_l3_v6 *v6, const char *key,
                         const char *value, struct dp_l3_config_stats *st)
{
    char buf[DP_L3_CFG_MAX_VALUE];
    char *tok[DP_L3_CFG_MAX_TOK];
    int n, i;

    if (strlen(value) >= sizeof(buf)) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
    strcpy(buf, value);

    /* dp.l3.route6.<id> = <prefix>/<len> [via <gw>] dev <egress> */
    if (strncmp(key, "dp.l3.route6.", 13) == 0) {
        uint8_t prefix[16], gw[16];
        uint8_t plen = 0;
        int have_dev = 0;
        uint16_t dev = 0;

        memset(gw, 0, sizeof(gw));          /* no "via" == directly connected */

        n = dp_l3_tokenise(buf, tok, DP_L3_CFG_MAX_TOK);
        if (n < 3) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        if (dp_l3_parse_prefix6(tok[0], prefix, &plen) != 0) {
            st->errors++; return DP_L3_CFG_ERR_ADDR;
        }

        if (strcmp(tok[1], "nexthop") == 0)
            return cfg6_route_multipath(v6, prefix, plen, tok, n, st);

        for (i = 1; i + 1 < n; i += 2) {
            if (strcmp(tok[i], "via") == 0) {
                if (dp_l3_parse_ipv6(tok[i + 1], gw) != 0) {
                    st->errors++; return DP_L3_CFG_ERR_ADDR;
                }
            } else if (strcmp(tok[i], "dev") == 0) {
                if (dp_l3_parse_dev(tok[i + 1], &dev) != 0) {
                    st->errors++; return DP_L3_CFG_ERR_SYNTAX;
                }
                have_dev = 1;
            } else {
                st->errors++; return DP_L3_CFG_ERR_SYNTAX;
            }
        }
        if (!have_dev) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }

        if (dp_l3_route6_add(v6, prefix, plen, gw, dev) != DP_L3_OK) {
            st->errors++; return DP_L3_CFG_ERR_APPLY;
        }
        st->routes++;
        return DP_L3_CFG_OK;
    }

    /* dp.l3.neigh6.<id> = <ip> lladdr <mac> */
    if (strncmp(key, "dp.l3.neigh6.", 13) == 0) {
        uint8_t ip[16], mac[6];

        n = dp_l3_tokenise(buf, tok, DP_L3_CFG_MAX_TOK);
        if (n != 3 || strcmp(tok[1], "lladdr") != 0) {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }
        if (dp_l3_parse_ipv6(tok[0], ip) != 0) { st->errors++; return DP_L3_CFG_ERR_ADDR; }
        if (dp_l3_parse_mac(tok[2], mac) != 0) { st->errors++; return DP_L3_CFG_ERR_ADDR; }

        if (dp_l3_neigh6_add(v6, ip, mac) != DP_L3_OK) {
            st->errors++; return DP_L3_CFG_ERR_APPLY;
        }
        st->neigh++;
        return DP_L3_CFG_OK;
    }

    /* dp.l3.iface6.<egress>.mac = <mac>, or .ip = <ip> */
    if (strncmp(key, "dp.l3.iface6.", 13) == 0) {
        const char *idp = key + 13;
        const char *dot;
        char devbuf[16];
        uint16_t dev;
        size_t dlen;

        dot = strchr(idp, '.');
        if (!dot) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        dlen = (size_t)(dot - idp);
        if (dlen == 0 || dlen >= sizeof(devbuf)) {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }
        memcpy(devbuf, idp, dlen);
        devbuf[dlen] = '\0';
        if (dp_l3_parse_dev(devbuf, &dev) != 0) {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }

        if (strcmp(dot, ".mac") == 0) {
            uint8_t mac[6];
            if (dp_l3_parse_mac(buf, mac) != 0) {
                st->errors++; return DP_L3_CFG_ERR_ADDR;
            }
            if (dp_l3_iface6_set_mac(v6, dev, mac) != DP_L3_OK) {
                st->errors++; return DP_L3_CFG_ERR_APPLY;
            }
        } else if (strcmp(dot, ".ip") == 0) {
            uint8_t ip[16];
            if (dp_l3_parse_ipv6(buf, ip) != 0) {
                st->errors++; return DP_L3_CFG_ERR_ADDR;
            }
            if (dp_l3_iface6_set_ip(v6, dev, ip) != DP_L3_OK) {
                st->errors++; return DP_L3_CFG_ERR_APPLY;
            }
        } else {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }
        st->ifaces++;
        return DP_L3_CFG_OK;
    }

    st->ignored++;             /* not ours: the v4 keys, cp.*, all.* etc. */
    return DP_L3_CFG_SKIP;
}

/* ---- whole file ------------------------------------------------------- */

int dp_l3_v6_config_apply(struct dp_l3_v6 *v6, const char *path,
                          struct dp_l3_config_stats *st)
{
    char line[DP_L3_CFG_MAX_LINE];
    FILE *fh;

    memset(st, 0, sizeof(*st));
    fh = fopen(path, "r");
    if (!fh) return DP_L3_CFG_ERR_OPEN;

    /* The relayed file is the whole config for this node, so most keys belong
     * to somebody else -- including every v4 L3 key. Unknown keys are counted
     * and skipped, never an error: a DP that refused to start because the MP
     * added a knob it has not heard of would be worse than one that ignores
     * it. */
    while (fgets(line, sizeof(line), fh)) {
        char *eq, *nl;

        nl = strpbrk(line, "\r\n");
        if (nl) *nl = '\0';
        if (line[0] == '\0' || line[0] == '#') continue;

        eq = strchr(line, '=');
        if (!eq) continue;
        *eq = '\0';

        dp_l3_v6_config_line(v6, line, eq + 1, st);
    }

    fclose(fh);
    return DP_L3_CFG_OK;
}
