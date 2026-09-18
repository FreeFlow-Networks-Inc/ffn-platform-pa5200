/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_parse.c -- text -> address, shared by the v4 and v6 config layers.
 *
 * See ffn_dp_l3_parse.h for why v4 lands in host order and v6 in wire bytes.
 */
#include <string.h>
#include <stdlib.h>

#include "ffn_dp_l3_parse.h"

/* ---- v4 ---------------------------------------------------------------- */

int dp_l3_parse_ipv4(const char *s, uint32_t *out)
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

int dp_l3_parse_prefix(const char *s, uint32_t *ip, uint8_t *len)
{
    char buf[64];
    char *slash;
    long l;

    if (strlen(s) >= sizeof(buf)) return -1;
    strcpy(buf, s);
    slash = strchr(buf, '/');
    if (!slash) return -1;
    *slash = '\0';

    if (dp_l3_parse_ipv4(buf, ip) != 0) return -1;

    if (slash[1] == '\0') return -1;
    l = strtol(slash + 1, &slash, 10);
    if (*slash != '\0' || l < 0 || l > 32) return -1;
    *len = (uint8_t)l;
    return 0;
}

/* ---- shared ------------------------------------------------------------ */

static int hex_val(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int dp_l3_parse_mac(const char *s, uint8_t mac[6])
{
    int i;
    for (i = 0; i < 6; i++) {
        int hi, lo;
        hi = hex_val((unsigned char)s[0]);
        lo = hex_val((unsigned char)s[1]);
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

int dp_l3_tokenise(char *s, char **tok, int max)
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

int dp_l3_parse_dev(const char *s, uint16_t *out)
{
    char *end;
    long dev;

    if (*s < '0' || *s > '9') return -1;      /* strtol would accept " +3" */
    dev = strtol(s, &end, 10);
    if (*end != '\0' || dev < 0 || dev > 0xFFFF) return -1;
    *out = (uint16_t)dev;
    return 0;
}

/* ---- v6 ---------------------------------------------------------------- */

/* RFC 4291 text form, into wire bytes.
 *
 * Written out rather than handed to inet_pton() for the same reason the rest
 * of this file is: the dataplane runs freestanding on the OCTEON where the
 * resolver headers are not something to depend on, and a parser that rejects
 * is worth more here than one that is merely convenient. The awkward cases are
 * all rejections, and each is a real way a hand-edited config goes wrong:
 * two "::" runs, a lone leading or trailing ':', a five-digit group, nine
 * groups, or a dotted quad anywhere but last.
 */
int dp_l3_parse_ipv6(const char *s, uint8_t out[16])
{
    uint16_t g[8];
    int ng = 0;           /* groups actually written */
    int gap = -1;         /* index in g[] that "::" stands before, -1 = none */
    const char *p = s;
    int i, o, zeros;

    if (!s || !*s) return -1;

    if (*p == ':') {
        if (p[1] != ':') return -1;           /* a lone leading ':' */
        gap = 0;
        p += 2;
        if (*p == '\0') {                     /* "::" -- the unspecified address */
            memset(out, 0, 16);
            return 0;
        }
    }

    for (;;) {
        const char *q;
        uint32_t v = 0;
        int nd = 0;

        /* A dotted quad is only legal as the FINAL element, so find the end of
         * this group first and look for a '.' inside it. */
        for (q = p; *q && *q != ':'; q++) { }
        if (memchr(p, '.', (size_t)(q - p)) != NULL) {
            uint32_t v4;
            if (*q != '\0') return -1;        /* something followed it */
            if (ng > 6) return -1;            /* no room for two more groups */
            if (dp_l3_parse_ipv4(p, &v4) != 0) return -1;
            g[ng++] = (uint16_t)(v4 >> 16);
            g[ng++] = (uint16_t)(v4 & 0xFFFFu);
            break;
        }

        while (hex_val((unsigned char)*p) >= 0) {
            if (++nd > 4) return -1;          /* a group is at most 16 bits */
            v = (v << 4) | (uint32_t)hex_val((unsigned char)*p);
            p++;
        }
        if (nd == 0) return -1;               /* empty group: ":::" or "1::: " */
        if (ng >= 8) return -1;               /* a ninth group */
        g[ng++] = (uint16_t)v;

        if (*p == '\0') break;
        if (*p != ':') return -1;             /* junk after a group */
        p++;
        if (*p == ':') {                      /* "::" */
            if (gap >= 0) return -1;          /* only one run may be elided */
            gap = ng;
            p++;
            if (*p == '\0') break;
        } else if (*p == '\0') {
            return -1;                        /* trailing single ':' */
        }
    }

    if (gap < 0) {
        if (ng != 8) return -1;               /* uncompressed must be exact */
    } else if (ng >= 8) {
        return -1;                            /* "::" must elide at least one */
    }

    zeros = 8 - ng;
    o = 0;
    for (i = 0; i < ng; i++) {
        if (i == gap) {
            int k;
            for (k = 0; k < zeros; k++) { out[o++] = 0; out[o++] = 0; }
        }
        out[o++] = (uint8_t)(g[i] >> 8);
        out[o++] = (uint8_t)(g[i] & 0xFFu);
    }
    if (gap == ng) {                          /* trailing "::" */
        int k;
        for (k = 0; k < zeros; k++) { out[o++] = 0; out[o++] = 0; }
    }
    return 0;
}

int dp_l3_parse_prefix6(const char *s, uint8_t out[16], uint8_t *len)
{
    char buf[80];
    char *slash;
    long l;

    if (!s || strlen(s) >= sizeof(buf)) return -1;
    strcpy(buf, s);
    slash = strchr(buf, '/');
    if (!slash) return -1;
    *slash = '\0';

    if (dp_l3_parse_ipv6(buf, out) != 0) return -1;

    if (slash[1] == '\0') return -1;
    l = strtol(slash + 1, &slash, 10);
    if (*slash != '\0' || l < 0 || l > 128) return -1;
    *len = (uint8_t)l;
    return 0;
}
