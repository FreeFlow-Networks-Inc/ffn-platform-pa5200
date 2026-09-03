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

/* ---- primitive parsers ------------------------------------------------ */

/* "10.1.2.3" -> host order. Returns 0 on success, -1 on malformed input.
 * Rejects rather than saturating: a typo must not silently become a route. */
static int parse_ipv4(const char *s, uint32_t *out)
{
    uint32_t v = 0;
    int octet, i;

    for (i = 0; i < 4; i++) {
        int digits = 0;
        octet = 0;
        if (*s < '0' || *s > '9') return -1;
        while (*s >= '0' && *s <= '9') {
            octet = octet * 10 + (*s - '0');
            if (octet > 255) return -1;
            s++;
            if (++digits > 3) return -1;
        }
        v = (v << 8) | (uint32_t)octet;
        if (i < 3) {
            if (*s != '.') return -1;
            s++;
        }
    }
    if (*s != '\0') return -1;
    *out = v;
    return 0;
}

/* "10.1.0.0/16" -> prefix (host order) + length. */
static int parse_prefix(const char *s, uint32_t *ip, uint8_t *len)
{
    char buf[64];
    char *slash;
    long l;

    if (strlen(s) >= sizeof(buf)) return -1;
    strcpy(buf, s);
    slash = strchr(buf, '/');
    if (!slash) return -1;
    *slash = '\0';

    if (parse_ipv4(buf, ip) != 0) return -1;

    if (slash[1] == '\0') return -1;
    l = strtol(slash + 1, &slash, 10);
    if (*slash != '\0' || l < 0 || l > 32) return -1;
    *len = (uint8_t)l;
    return 0;
}

static int hex_val(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* "02:aa:bb:cc:dd:ee" -> 6 wire bytes. */
static int parse_mac(const char *s, uint8_t mac[6])
{
    int i;
    for (i = 0; i < 6; i++) {
        int hi, lo;
        hi = hex_val(s[0]); lo = hex_val(s[1]);
        if (hi < 0 || lo < 0) return -1;
        mac[i] = (uint8_t)((hi << 4) | lo);
        s += 2;
        if (i < 5) {
            if (*s != ':' && *s != '-') return -1;
            s++;
        }
    }
    return *s == '\0' ? 0 : -1;
}


/* Split a value into whitespace-separated tokens, in place. */
static int tokenise(char *s, char **tok, int max)
{
    int n = 0;
    while (*s && n < max) {
        while (*s == ' ' || *s == '\t') s++;
        if (!*s) break;
        tok[n++] = s;
        while (*s && *s != ' ' && *s != '\t') s++;
        if (*s) *s++ = '\0';
    }
    return n;
}

/* ---- one key = one object --------------------------------------------- */

int dp_l3_config_line(struct dp_l3 *l3, const char *key, const char *value,
                      struct dp_l3_config_stats *st)
{
    char buf[256];
    char *tok[8];
    int n, i;

    if (strlen(value) >= sizeof(buf)) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
    strcpy(buf, value);

    /* dp.l3.route.<id> = <prefix>/<len> [via <gw>] dev <egress> */
    if (strncmp(key, "dp.l3.route.", 12) == 0) {
        uint32_t prefix = 0, gw = 0;
        uint8_t plen = 0;
        long dev = -1;

        n = tokenise(buf, tok, 8);
        if (n < 3) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }
        if (parse_prefix(tok[0], &prefix, &plen) != 0) {
            st->errors++; return DP_L3_CFG_ERR_ADDR;
        }
        for (i = 1; i + 1 < n; i += 2) {
            if (strcmp(tok[i], "via") == 0) {
                if (parse_ipv4(tok[i + 1], &gw) != 0) {
                    st->errors++; return DP_L3_CFG_ERR_ADDR;
                }
            } else if (strcmp(tok[i], "dev") == 0) {
                char *end;
                dev = strtol(tok[i + 1], &end, 10);
                if (*end != '\0' || dev < 0 || dev > 0xFFFF) {
                    st->errors++; return DP_L3_CFG_ERR_SYNTAX;
                }
            } else {
                st->errors++; return DP_L3_CFG_ERR_SYNTAX;
            }
        }
        if (dev < 0) { st->errors++; return DP_L3_CFG_ERR_SYNTAX; }

        if (dp_l3_route_add(l3, prefix, plen, gw, (uint16_t)dev) != DP_L3_OK) {
            st->errors++; return DP_L3_CFG_ERR_APPLY;
        }
        st->routes++;
        return DP_L3_CFG_OK;
    }

    /* dp.l3.neigh.<id> = <ip> lladdr <mac> */
    if (strncmp(key, "dp.l3.neigh.", 12) == 0) {
        uint32_t ip = 0;
        uint8_t mac[6];

        n = tokenise(buf, tok, 8);
        if (n != 3 || strcmp(tok[1], "lladdr") != 0) {
            st->errors++; return DP_L3_CFG_ERR_SYNTAX;
        }
        if (parse_ipv4(tok[0], &ip) != 0) { st->errors++; return DP_L3_CFG_ERR_ADDR; }
        if (parse_mac(tok[2], mac) != 0)  { st->errors++; return DP_L3_CFG_ERR_ADDR; }

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
        if (parse_mac(buf, mac) != 0) { st->errors++; return DP_L3_CFG_ERR_ADDR; }
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
    char line[512];
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
