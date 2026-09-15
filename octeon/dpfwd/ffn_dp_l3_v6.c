/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_v6.c -- IPv6 FIB, neighbours and header rewrite.
 *
 * See ffn_dp_l3_v6.h for why addresses are stored as wire bytes and why this
 * is a separate module from the v4 path.
 */
#include <stdlib.h>
#include <string.h>

#include "ffn_dp_l3_v6.h"
/* For DP_ERR_STATE, which is the shared "operation invalid in this state"
 * code. ffn_dp_l3.c includes this for the same reason; ffn_dp_oct.h is
 * architecture-neutral despite the name, so it does not compromise the
 * no-chip-headers rule the v6 header states. */
#include "ffn_dp_oct.h"

/* --- small helpers -------------------------------------------------------- */

void dp_l3_v6_mask(uint8_t addr[16], uint8_t plen)
{
    unsigned full, rem, i;

    if (plen >= 128) return;
    full = plen / 8u;
    rem  = plen % 8u;

    if (rem)
        addr[full] = (uint8_t)(addr[full] & (0xFFu << (8u - rem)));
    for (i = full + (rem ? 1u : 0u); i < 16u; i++)
        addr[i] = 0;
}

/* FNV-1a over the masked prefix plus the length.
 *
 * The length is folded in so that 2001:db8::/32 and 2001:db8::/48 land in
 * different slots even though the masked bytes of the shorter one are a prefix
 * of the longer. Without it the two collide on every probe. */
static uint32_t addr_hash(const uint8_t *a, uint8_t extra)
{
    uint32_t h = 2166136261u;
    unsigned i;

    for (i = 0; i < 16u; i++) {
        h ^= a[i];
        h *= 16777619u;
    }
    h ^= extra;
    h *= 16777619u;
    /* Avalanche the low bits: slots are a power of two and are indexed with a
     * mask, so poor low bits would cluster every route into a few buckets. */
    h ^= h >> 15;
    h *= 2246822519u;
    h ^= h >> 13;
    return h;
}

static uint32_t round_pow2(uint32_t v)
{
    uint32_t p = 1;

    while (p < v && p < (1u << 30)) p <<= 1;
    return p;
}

static void len_bit_set(struct dp_l3_v6 *v6, uint8_t plen)
{
    v6->len_bitmap[plen >> 5] |= 1u << (plen & 31u);
}

static int len_bit_test(const struct dp_l3_v6 *v6, uint8_t plen)
{
    return (v6->len_bitmap[plen >> 5] >> (plen & 31u)) & 1u;
}

static void len_bitmap_rebuild(struct dp_l3_v6 *v6)
{
    uint32_t i;

    memset(v6->len_bitmap, 0, sizeof(v6->len_bitmap));
    for (i = 0; i < v6->route_slots; i++)
        if (v6->routes[i].used)
            len_bit_set(v6, v6->routes[i].plen);
}

/* --- lifecycle ------------------------------------------------------------ */

int dp_l3_v6_init(struct dp_l3_v6 *v6, uint32_t route_slots, uint32_t neigh_slots)
{
    if (!v6) return DP_L3_ERR_RANGE;
    memset(v6, 0, sizeof(*v6));

    if (route_slots == 0) route_slots = 256;
    if (neigh_slots == 0) neigh_slots = 256;
    if (route_slots > DP_L3_V6_MAX_ROUTES) route_slots = DP_L3_V6_MAX_ROUTES;
    if (neigh_slots > DP_L3_V6_MAX_NEIGH)  neigh_slots = DP_L3_V6_MAX_NEIGH;
    route_slots = round_pow2(route_slots);
    neigh_slots = round_pow2(neigh_slots);

    v6->routes = calloc(route_slots, sizeof(*v6->routes));
    v6->neigh  = calloc(neigh_slots, sizeof(*v6->neigh));
    if (!v6->routes || !v6->neigh) {
        free(v6->routes); free(v6->neigh);
        v6->routes = NULL; v6->neigh = NULL;
        return DP_L3_ERR_FULL;
    }
    v6->route_slots = route_slots; v6->route_mask = route_slots - 1u;
    v6->neigh_slots = neigh_slots; v6->neigh_mask = neigh_slots - 1u;
    return DP_L3_OK;
}

void dp_l3_v6_fini(struct dp_l3_v6 *v6)
{
    if (!v6) return;
    free(v6->routes); free(v6->neigh);
    v6->routes = NULL; v6->neigh = NULL;
    v6->route_slots = v6->neigh_slots = 0;
    v6->route_count = v6->neigh_count = 0;
}

void dp_l3_v6_flush(struct dp_l3_v6 *v6)
{
    if (!v6) return;
    if (v6->routes) memset(v6->routes, 0, v6->route_slots * sizeof(*v6->routes));
    if (v6->neigh)  memset(v6->neigh,  0, v6->neigh_slots * sizeof(*v6->neigh));
    memset(v6->len_bitmap, 0, sizeof(v6->len_bitmap));
    memset(v6->ecmp, 0, sizeof(v6->ecmp));
    v6->route_count = v6->neigh_count = v6->ecmp_count = 0;
}

/* --- routes --------------------------------------------------------------- */

/* Open addressing with linear probing, bounded by the table size so a full
 * table terminates instead of spinning. */
static struct dp_l3_route6 *route_slot(struct dp_l3_v6 *v6, const uint8_t *prefix,
                                       uint8_t plen, int for_insert)
{
    uint32_t i, idx = addr_hash(prefix, plen) & v6->route_mask;

    for (i = 0; i < v6->route_slots; i++) {
        struct dp_l3_route6 *r = &v6->routes[(idx + i) & v6->route_mask];

        if (r->used && r->plen == plen && memcmp(r->prefix, prefix, 16) == 0)
            return r;
        if (!r->used)
            return for_insert ? r : NULL;
    }
    return NULL;
}

int dp_l3_route6_add(struct dp_l3_v6 *v6, const uint8_t prefix[16], uint8_t plen,
                     const uint8_t nexthop[16], uint16_t egress)
{
    struct dp_l3_route6 *r;
    uint8_t key[16];
    int fresh;

    if (!v6 || !prefix) return DP_L3_ERR_RANGE;
    if (plen > 128)     return DP_L3_ERR_RANGE;
    if (!v6->routes)    return DP_ERR_STATE;

    memcpy(key, prefix, 16);
    dp_l3_v6_mask(key, plen);          /* normalise, as the v4 path does */

    r = route_slot(v6, key, plen, 1);
    if (!r) return DP_L3_ERR_FULL;

    fresh = !r->used;
    if (fresh && v6->route_count + 1u > (v6->route_slots * 3u) / 4u)
        return DP_L3_ERR_FULL;         /* keep probe chains short */

    memcpy(r->prefix, key, 16);
    if (nexthop) memcpy(r->nexthop, nexthop, 16);
    else         memset(r->nexthop, 0, 16);
    r->egress = egress;
    r->plen = plen;
    /* Clear any group reference: a single-next-hop add must not keep taking
     * an ECMP group and silently ignore the nexthop just supplied. */
    r->ecmp = 0;
    r->used = 1;
    if (fresh) v6->route_count++;
    len_bit_set(v6, plen);
    return DP_L3_OK;
}

int dp_l3_route6_del(struct dp_l3_v6 *v6, const uint8_t prefix[16], uint8_t plen)
{
    struct dp_l3_route6 *r;
    uint8_t key[16];

    if (!v6 || !prefix || plen > 128) return DP_L3_ERR_RANGE;
    if (!v6->routes) return DP_ERR_STATE;

    memcpy(key, prefix, 16);
    dp_l3_v6_mask(key, plen);
    r = route_slot(v6, key, plen, 0);
    if (!r || !r->used) return DP_L3_ERR_NOROUTE;

    memset(r, 0, sizeof(*r));
    if (v6->route_count) v6->route_count--;
    /* A cleared slot can break a probe chain, and the length may now be empty,
     * so the bitmap is rebuilt rather than guessed at. */
    len_bitmap_rebuild(v6);
    return DP_L3_OK;
}

/* --- neighbours ----------------------------------------------------------- */

static struct dp_l3_neigh6 *neigh_slot(struct dp_l3_v6 *v6, const uint8_t *ip,
                                       int for_insert)
{
    uint32_t i, idx = addr_hash(ip, 0) & v6->neigh_mask;

    for (i = 0; i < v6->neigh_slots; i++) {
        struct dp_l3_neigh6 *e = &v6->neigh[(idx + i) & v6->neigh_mask];

        if (e->used && memcmp(e->ip, ip, 16) == 0) return e;
        if (!e->used) return for_insert ? e : NULL;
    }
    return NULL;
}

int dp_l3_neigh6_add(struct dp_l3_v6 *v6, const uint8_t ip[16], const uint8_t mac[6])
{
    struct dp_l3_neigh6 *e;

    if (!v6 || !ip || !mac) return DP_L3_ERR_RANGE;
    if (!v6->neigh) return DP_ERR_STATE;

    e = neigh_slot(v6, ip, 1);
    if (!e) return DP_L3_ERR_FULL;
    if (!e->used) {
        if (v6->neigh_count + 1u > (v6->neigh_slots * 3u) / 4u)
            return DP_L3_ERR_FULL;
        v6->neigh_count++;
    }
    memcpy(e->ip, ip, 16);
    memcpy(e->mac, mac, 6);
    e->state = DP_NEIGH_REACHABLE;
    e->probes = 0;
    e->used = 1;
    return DP_L3_OK;
}

int dp_l3_neigh6_del(struct dp_l3_v6 *v6, const uint8_t ip[16])
{
    struct dp_l3_neigh6 *e;

    if (!v6 || !ip) return DP_L3_ERR_RANGE;
    if (!v6->neigh) return DP_ERR_STATE;
    e = neigh_slot(v6, ip, 0);
    if (!e || !e->used) return DP_L3_ERR_NOROUTE;
    memset(e, 0, sizeof(*e));
    if (v6->neigh_count) v6->neigh_count--;
    return DP_L3_OK;
}

/* --- interfaces ----------------------------------------------------------- */

int dp_l3_iface6_set_mac(struct dp_l3_v6 *v6, uint16_t egress, const uint8_t mac[6])
{
    if (!v6 || !mac || egress >= DP_L3_MAX_IFACES) return DP_L3_ERR_RANGE;
    memcpy(v6->iface[egress].mac, mac, 6);
    v6->iface[egress].used = 1;
    return DP_L3_OK;
}

int dp_l3_iface6_set_ip(struct dp_l3_v6 *v6, uint16_t egress, const uint8_t ip[16])
{
    if (!v6 || !ip || egress >= DP_L3_MAX_IFACES) return DP_L3_ERR_RANGE;
    memcpy(v6->iface[egress].ip, ip, 16);
    v6->iface[egress].used = 1;
    return DP_L3_OK;
}

/* --- ECMP ----------------------------------------------------------------- */

int dp_l3_v6_ecmp_add(struct dp_l3_v6 *v6, const uint8_t nexthop[][16],
                      const uint16_t *egress, uint8_t n)
{
    uint32_t g;
    uint8_t i;

    if (!v6 || !nexthop || !egress) return DP_L3_ERR_RANGE;
    if (n == 0 || n > DP_L3_ECMP_MAX) return DP_L3_ERR_RANGE;

    for (g = 1; g < DP_L3_MAX_ECMP_GROUPS; g++)
        if (!v6->ecmp[g].used) break;
    if (g >= DP_L3_MAX_ECMP_GROUPS) return DP_L3_ERR_FULL;

    /*
     * struct dp_l3_ecmp is the v4 one, so its nexthop[] is uint32_t and cannot
     * hold a 128-bit address. Rather than duplicate the struct, the group here
     * stores only the EGRESS per member and the next hop is taken from the
     * route's own nexthop field.
     *
     * That makes v6 ECMP "same gateway, several egress ports", which is what a
     * LAG-like spread over parallel links looks like. Spreading across
     * DIFFERENT v6 gateways needs a 128-bit member array and is deliberately
     * not pretended at here -- returning a wrong next hop would be worse than
     * not offering the feature.
     */
    memset(&v6->ecmp[g], 0, sizeof(v6->ecmp[g]));
    for (i = 0; i < n; i++) {
        v6->ecmp[g].egress[i] = egress[i];
        v6->ecmp[g].nexthop[i] = 0;      /* unused for v6; see above */
    }
    v6->ecmp[g].n = n;
    v6->ecmp[g].used = 1;
    v6->ecmp_count++;
    return (int)g;
}

int dp_l3_route6_add_ecmp(struct dp_l3_v6 *v6, const uint8_t prefix[16],
                          uint8_t plen, uint16_t group)
{
    int rc;
    struct dp_l3_route6 *r;
    uint8_t key[16];

    if (!v6 || !prefix || plen > 128) return DP_L3_ERR_RANGE;
    if (group == 0 || group >= DP_L3_MAX_ECMP_GROUPS) return DP_L3_ERR_RANGE;
    if (!v6->ecmp[group].used) return DP_L3_ERR_RANGE;

    /* Insert with no next hop, then attach the group: reuses the one code path
     * that does masking, capacity and bitmap bookkeeping. */
    rc = dp_l3_route6_add(v6, prefix, plen, NULL, 0);
    if (rc != DP_L3_OK) return rc;

    memcpy(key, prefix, 16);
    dp_l3_v6_mask(key, plen);
    r = route_slot(v6, key, plen, 0);
    if (!r || !r->used) return DP_ERR_STATE;
    r->ecmp = group;
    return DP_L3_OK;
}

/* --- lookup --------------------------------------------------------------- */

int dp_l3_lookup6(struct dp_l3_v6 *v6, const uint8_t dst[16],
                  struct dp_l3_nh6 *nh)
{
    /* Per-destination spread: hash the address itself. Callers with a 5-tuple
     * should use dp_l3_lookup6_hash() for per-flow spread. */
    return dp_l3_lookup6_hash(v6, dst, addr_hash(dst, 0), nh);
}

int dp_l3_lookup6_hash(struct dp_l3_v6 *v6, const uint8_t dst[16],
                       uint32_t flow_hash, struct dp_l3_nh6 *nh)
{
    int len;

    if (!v6 || !dst || !nh) return DP_L3_ERR_RANGE;
    if (!v6->routes) return DP_ERR_STATE;
    v6->stat_lookup++;
    memset(nh, 0, sizeof(*nh));

    /* Longest-prefix match by construction: /128 down to /0, first hit wins. */
    for (len = 128; len >= 0; len--) {
        struct dp_l3_route6 *r;
        struct dp_l3_neigh6 *e;
        uint8_t key[16];

        if (!len_bit_test(v6, (uint8_t)len)) continue;
        memcpy(key, dst, 16);
        dp_l3_v6_mask(key, (uint8_t)len);
        r = route_slot(v6, key, (uint8_t)len, 0);
        if (!r || !r->used) continue;

        if (r->ecmp) {
            const struct dp_l3_ecmp *g = &v6->ecmp[r->ecmp];
            uint32_t m;

            if (!g->used || g->n == 0) {
                v6->stat_noroute++;
                return DP_L3_ERR_NOROUTE;
            }
            /* Deterministic per (hash, group): a split flow would reorder. */
            m = flow_hash % g->n;
            nh->egress = g->egress[m];
        } else {
            nh->egress = r->egress;
        }

        /* An all-zero next hop means directly connected: the destination is
         * the next hop, exactly as nexthop == 0 means in the v4 table. */
        {
            static const uint8_t zero[16] = { 0 };
            if (memcmp(r->nexthop, zero, 16) == 0)
                memcpy(nh->nexthop, dst, 16);
            else
                memcpy(nh->nexthop, r->nexthop, 16);
        }

        e = neigh_slot(v6, nh->nexthop, 0);
        if (e && e->used && e->state == DP_NEIGH_REACHABLE) {
            memcpy(nh->mac, e->mac, 6);
            nh->have_mac = 1;
        } else {
            v6->stat_noneigh++;
        }
        v6->stat_hit++;
        return DP_L3_OK;
    }

    v6->stat_noroute++;
    return DP_L3_ERR_NOROUTE;
}

/* --- rewrite -------------------------------------------------------------- */

/* Offset of the IPv6 header, skipping one optional 802.1Q tag.
 *
 * Returns 0 if this is not IPv6. The IPv6 header is a fixed 40 bytes, so
 * unlike v4 there is no IHL to read and no variable-length options to skip --
 * extension headers live AFTER the 40 bytes and do not move Hop Limit. */
static uint32_t ip6_offset(const uint8_t *pkt, uint32_t len)
{
    uint16_t eth;

    if (len < 14u + 40u) return 0;
    eth = (uint16_t)((pkt[12] << 8) | pkt[13]);
    if (eth == 0x8100u) {
        if (len < 18u + 40u) return 0;
        eth = (uint16_t)((pkt[16] << 8) | pkt[17]);
        if (eth != 0x86DDu) return 0;
        return 18u;
    }
    if (eth != 0x86DDu) return 0;
    return 14u;
}

int dp_l3_rewrite6(struct dp_l3_v6 *v6, uint8_t *pkt, uint32_t len,
                   const struct dp_l3_nh6 *nh)
{
    uint32_t off;
    uint8_t hop;

    if (!v6 || !pkt || !nh) return DP_L3_ERR_RANGE;
    if (!nh->have_mac) return DP_L3_ERR_NONEIGH;

    off = ip6_offset(pkt, len);
    if (off == 0) return DP_L3_ERR_SHORT;

    /* Hop Limit is byte 7 of the IPv6 header. NOT byte 8 -- that is v4's TTL,
     * and copying the v4 rewrite without changing this decrements the wrong
     * field (the low byte of the payload length) while leaving Hop Limit
     * untouched, so packets would loop forever and lengths would be corrupt. */
    hop = pkt[off + 7u];
    if (hop <= 1u) {
        v6->stat_ttl++;
        return DP_L3_ERR_TTL;
    }
    pkt[off + 7u] = (uint8_t)(hop - 1u);

    /* No checksum. IPv6 has no header checksum, which is why there is no
     * incremental update here and nothing for an endianness mistake to
     * corrupt. The transport checksum covers the addresses, but forwarding
     * changes neither address, so it stays valid. */

    memcpy(pkt, nh->mac, 6);                       /* new destination MAC */
    if (nh->egress < DP_L3_MAX_IFACES && v6->iface[nh->egress].used)
        memcpy(pkt + 6, v6->iface[nh->egress].mac, 6);   /* our source MAC */

    return DP_L3_OK;
}
