/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_config.c -- populate the FIB from the config the MP relays down.
 *
 * The chain this closes:
 *
 *   MP /etc/ffn/config.env      (ffn_cfgd, source of truth)
 *     -> PCIe virtual ethernet  (ffn_cfgagent on the CP)
 *       -> PCIe mailbox         (store-and-forward, no IP path to the DP)
 *         -> DP /etc/ffn/dp.env
 *           -> here             -> dp_l3_route_add() / dp_l3_neigh_add()
 *
 * Until now dp_l3_route_add() had no caller outside the unit tests, so the
 * relay delivered keys into a void. This is the apply step.
 *
 * Syntax, deliberately iproute2-shaped so it reads the way an operator expects:
 *
 *   dp.l3.route.<id>=<prefix>/<len> [via <gateway>] dev <egress>
 *   dp.l3.neigh.<id>=<ip> lladdr <aa:bb:cc:dd:ee:ff>
 *   dp.l3.iface.<egress>.mac=<aa:bb:cc:dd:ee:ff>
 *
 * A route with no "via" is directly connected, which dp_l3_route_add() encodes
 * as nexthop 0 and the lookup resolves to the destination itself.
 *
 * The <id> is only there to let distinct routes coexist as distinct keys in a
 * flat key/value namespace; nothing here depends on its value or ordering.
 *
 * Addresses are parsed to HOST order by hand rather than with inet_pton(),
 * which returns network order. Converting that back would mean a host-
 * endianness byte swap -- precisely the bug ffn_dp_oct.h warns about, which
 * looks correct on x86 and corrupts every address on the big-endian OCTEON.
 * Parsing straight to host order means there is no swap to get wrong.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ffn_dp_l3_config.h"
#include "ffn_dp_l3_parse.h"

/* ---- multipath --------------------------------------------------------- */

/* "<prefix> nexthop via <gw> dev <n> nexthop via <gw> dev <n> ..."
 *
 * tok[0] is the prefix and has already been parsed; parsing resumes at tok[1],
 * which the caller has confirmed is "nexthop".
 *
 * Each leg needs "dev" and may omit "via" -- a directly connected member, the
 * same meaning a via-less single path has. The legs are collected first and
 * the group is created only once every one of them parsed, so a typo in the
 * third leg does not leave a half-built group in the FIB with two members the
 * operator never asked for.
 */
static int cfg_route_multipath(struct dp_l3 *l3, uint32_t prefix, uint8_t plen,
                               char **tok, int n, struct dp_l3_config_stats *st)
{
    uint32_t nh[DP_L3_ECMP_MAX];
    uint16_t eg[DP_L3_ECMP_MAX];
    int legs = 0, i = 1, group;

    while (i < n) {
        int have_dev = 0;

        if (strcmp(tok[i], "nexthop") != 0) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        i++;
        if (legs >= (int)DP_L3_ECMP_MAX) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        nh[legs] = 0;
        eg[legs] = 0;

        while (i + 1 < n && strcmp(tok[i], "nexthop") != 0) {
            if (strcmp(tok[i], "via") == 0) {
                if (dp_l3_parse_ipv4(tok[i + 1], &nh[legs]) != 0) {
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

    group = dp_l3_ecmp_add(l3, nh, eg, (uint8_t)legs);
    if (group < 0) { st->errors++; return DP_L3_CFG_ERR_APPLY; }
    if (dp_l3_route_add_ecmp(l3, prefix, plen, (uint16_t)group) != DP_L3_OK) {
        st->errors++; return DP_L3_CFG_ERR_APPLY;
    }
    st->routes++;
    return DP_L3_CFG_OK;
}

/* ---- one key = one object --------------------------------------------- */

int dp_l3_config_line(struct dp_l3 *l3, const char *key, const char *value,
                      struct dp_l3_config_stats *st)
{
    char buf[DP_L3_CFG_MAX_VALUE];
    char *tok[DP_L3_CFG_MAX_TOK];
    int n, i;

    if (strlen(value) >= sizeof(buf)) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
    strcpy(buf, value);

    /* dp.l3.route.<id> = <prefix>/<len> [via <gw>] dev <egress>
     *
     * ...or the multipath form, which is iproute2's and means the same thing
     * there as it does here:
     *
     *   dp.l3.route.<id> = <prefix>/<len> nexthop via <gw> dev <n> \
     *                                     nexthop via <gw> dev <n>
     *
     * dp_l3_ecmp_add() existed with no caller outside the unit tests, so an
     * operator could not reach ECMP from the config at all. The single-path
     * form stays exactly as it was: a route with one path must not become a
     * one-member group, because that is a different object in the FIB and
     * would change what dp_l3_lookup() returns for existing configs.
     */
    if (strncmp(key, "dp.l3.route.", 12) == 0) {
        uint32_t prefix = 0, gw = 0;
        uint8_t plen = 0;
        int have_dev = 0;
        uint16_t dev = 0;

        n = dp_l3_tokenise(buf, tok, DP_L3_CFG_MAX_TOK);
        if (n < 3) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        if (dp_l3_parse_prefix(tok[0], &prefix, &plen) != 0) {
            st->errors++; return DP_L3_CFG_ERR_ADDR;
        }

        if (strcmp(tok[1], "nexthop") == 0)
            return cfg_route_multipath(l3, prefix, plen, tok, n, st);

        for (i = 1; i + 1 < n; i += 2) {
            if (strcmp(tok[i], "via") == 0) {
                if (dp_l3_parse_ipv4(tok[i + 1], &gw) != 0) {
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

        if (dp_l3_route_add(l3, prefix, plen, gw, dev) != DP_L3_OK) {
            st->errors++; return DP_L3_CFG_ERR_APPLY;
        }
        st->routes++;
        return DP_L3_CFG_OK;
    }

    /* dp.l3.neigh.<id> = <ip> lladdr <mac> */
    if (strncmp(key, "dp.l3.neigh.", 12) == 0) {
        uint32_t ip = 0;
        uint8_t mac[6];

        n = dp_l3_tokenise(buf, tok, DP_L3_CFG_MAX_TOK);
        if (n != 3 || strcmp(tok[1], "lladdr") != 0) {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }
        if (dp_l3_parse_ipv4(tok[0], &ip) != 0) { st->errors++; return DP_L3_CFG_ERR_ADDR; }
        if (dp_l3_parse_mac(tok[2], mac) != 0)  { st->errors++; return DP_L3_CFG_ERR_ADDR; }

        if (dp_l3_neigh_add(l3, ip, mac) != DP_L3_OK) {
            st->errors++; return DP_L3_CFG_ERR_APPLY;
        }
        st->neigh++;
        return DP_L3_CFG_OK;
    }

    /* dp.l3.iface.<egress>.mac = <mac> */
    if (strncmp(key, "dp.l3.iface.", 12) == 0) {
        const char *idp = key + 12;
        char *end;
        long dev;
        uint8_t mac[6];

        dev = strtol(idp, &end, 10);
        if (strcmp(end, ".mac") != 0 || dev < 0 || dev > 0xFFFF) {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }
        if (dp_l3_parse_mac(buf, mac) != 0) { st->errors++; return DP_L3_CFG_ERR_ADDR; }
        if (dp_l3_iface_set_mac(l3, (uint16_t)dev, mac) != DP_L3_OK) {
            st->errors++; return DP_L3_CFG_ERR_APPLY;
        }
        st->ifaces++;
        return DP_L3_CFG_OK;
    }

    st->ignored++;             /* not ours: cp.*, dp.inspect.*, all.* etc. */
    return DP_L3_CFG_SKIP;
}

/* ---- whole file ------------------------------------------------------- */

int dp_l3_config_apply(struct dp_l3 *l3, const char *path,
                       struct dp_l3_config_stats *st)
{
    char line[DP_L3_CFG_MAX_LINE];
    FILE *fh;

    memset(st, 0, sizeof(*st));
    fh = fopen(path, "r");
    if (!fh) return DP_L3_CFG_ERR_OPEN;

    /* The relayed file is the whole config for this node, so most keys belong
     * to somebody else. Unknown keys are counted and skipped, never an error:
     * a DP that refuses to boot because the MP added an inspection knob it has
     * never heard of would be worse than one that ignores it. */
    while (fgets(line, sizeof(line), fh)) {
        char *eq, *nl;
        nl = strpbrk(line, "\r\n");
        if (nl) *nl = '\0';
        if (line[0] == '#' || line[0] == '\0') continue;
        eq = strchr(line, '=');
        if (!eq) continue;
        *eq = '\0';
        dp_l3_config_line(l3, line, eq + 1, st);
    }
    fclose(fh);
    return DP_L3_CFG_OK;
}
