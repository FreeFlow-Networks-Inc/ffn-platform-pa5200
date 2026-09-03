/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3.c -- FFN dataplane L3: FIB, neighbours, header rewrite.
 * See ffn_dp_l3.h for the design and the endianness rule.
 */
#include <stdlib.h>
#include <string.h>

#include "ffn_dp_l3.h"
#include "ffn_dp_oct.h"

/* ---- small helpers ---------------------------------------------------- */

static uint32_t len_to_mask(uint8_t len)
{
    /* A shift by 32 is undefined in C, which is exactly the bug that makes a
     * default route match nothing on one compiler and everything on another. */
    return len == 0 ? 0u : (uint32_t)(0xFFFFFFFFu << (32 - len));
}

static uint32_t mix(uint32_t k)
{
    k ^= k >> 16;  k *= 0x7feb352du;
    k ^= k >> 15;  k *= 0x846ca68bu;
    k ^= k >> 16;
    return k;
}

static uint32_t route_hash(uint32_t prefix, uint8_t len)
{
    return mix(prefix ^ (0x9e3779b9u * (uint32_t)(len + 1)));
}

static void len_bit_set(struct dp_l3 *l3, uint8_t len)
{
    if (len < 32) l3->len_bitmap[0] |= (1u << len);
    else          l3->len_bitmap[1] |= 1u;
}

static int len_bit_test(const struct dp_l3 *l3, uint8_t len)
{
    return len < 32 ? !!(l3->len_bitmap[0] & (1u << len))
                    : !!(l3->len_bitmap[1] & 1u);
}

static void len_bitmap_rebuild(struct dp_l3 *l3)
{
    uint32_t i;
    l3->len_bitmap[0] = l3->len_bitmap[1] = 0;
    for (i = 0; i < l3->route_slots; i++)
        if (l3->routes[i].used)
            len_bit_set(l3, l3->routes[i].prefix_len);
}

static uint32_t round_pow2(uint32_t v)
{
    uint32_t p = 1;
    while (p < v && p < (1u << 30)) p <<= 1;
    return p;
}

/* ---- lifecycle -------------------------------------------------------- */

int dp_l3_init(struct dp_l3 *l3, uint32_t route_slots, uint32_t neigh_slots)
{
    memset(l3, 0, sizeof(*l3));

    if (route_slots == 0) route_slots = DP_L3_MAX_ROUTES;
    if (neigh_slots == 0) neigh_slots = DP_L3_MAX_NEIGH;
    route_slots = round_pow2(route_slots);
    neigh_slots = round_pow2(neigh_slots);

    l3->routes = calloc(route_slots, sizeof(*l3->routes));
    l3->neigh  = calloc(neigh_slots, sizeof(*l3->neigh));
    if (!l3->routes || !l3->neigh) {
        free(l3->routes); free(l3->neigh);
        l3->routes = NULL; l3->neigh = NULL;
        return DP_ERR_NOMEM;
    }
    l3->route_slots = route_slots; l3->route_mask = route_slots - 1;
    l3->neigh_slots = neigh_slots; l3->neigh_mask = neigh_slots - 1;
    return DP_L3_OK;
}

void dp_l3_fini(struct dp_l3 *l3)
{
    free(l3->routes); free(l3->neigh);
    memset(l3, 0, sizeof(*l3));
}

void dp_l3_flush(struct dp_l3 *l3)
{
    if (l3->routes) memset(l3->routes, 0, l3->route_slots * sizeof(*l3->routes));
    if (l3->neigh)  memset(l3->neigh,  0, l3->neigh_slots * sizeof(*l3->neigh));
    l3->route_count = l3->neigh_count = 0;
    l3->len_bitmap[0] = l3->len_bitmap[1] = 0;
}

/* ---- routes ----------------------------------------------------------- */

static struct dp_l3_route *route_slot(struct dp_l3 *l3, uint32_t prefix,
                                      uint8_t len, int for_insert)
{
    uint32_t i = route_hash(prefix, len) & l3->route_mask;
    uint32_t n;
    for (n = 0; n < l3->route_slots; n++) {
        struct dp_l3_route *r = &l3->routes[(i + n) & l3->route_mask];
        if (!r->used)
            return for_insert ? r : NULL;
        if (r->prefix == prefix && r->prefix_len == len)
            return r;
    }
    return NULL;
}

int dp_l3_route_add(struct dp_l3 *l3, uint32_t prefix, uint8_t prefix_len,
                    uint32_t nexthop, uint16_t egress)
{
    struct dp_l3_route *r;
    uint32_t mask;
    int fresh;

    if (prefix_len > 32) return DP_L3_ERR_RANGE;
    if (!l3->routes)     return DP_ERR_STATE;

    mask = len_to_mask(prefix_len);
    prefix &= mask;              /* normalise, so 10.0.0.5/24 == 10.0.0.0/24 */

    r = route_slot(l3, prefix, prefix_len, 1);
    if (!r) return DP_L3_ERR_FULL;

    fresh = !r->used;
    if (fresh && l3->route_count + 1 > (l3->route_slots * 3) / 4)
        return DP_L3_ERR_FULL;   /* keep probe chains short */

    r->prefix = prefix; r->mask = mask; r->prefix_len = prefix_len;
    r->nexthop = nexthop; r->egress = egress; r->used = 1;
    if (fresh) l3->route_count++;
    len_bit_set(l3, prefix_len);
    return DP_L3_OK;
}

int dp_l3_route_del(struct dp_l3 *l3, uint32_t prefix, uint8_t prefix_len)
{
    struct dp_l3_route *r;
    uint32_t start, n;

    if (prefix_len > 32 || !l3->routes) return DP_L3_ERR_RANGE;
    prefix &= len_to_mask(prefix_len);

    r = route_slot(l3, prefix, prefix_len, 0);
    if (!r || !r->used) return DP_L3_ERR_NOROUTE;

    /* Open addressing: deleting mid-chain would strand entries behind the
     * hole, so clear this slot and reinsert the rest of the cluster. Deletes
     * happen at control-plane rate, so the cost is irrelevant next to getting
     * a silently unreachable route. */
    start = (uint32_t)(r - l3->routes);
    memset(r, 0, sizeof(*r));
    l3->route_count--;

    for (n = 1; n < l3->route_slots; n++) {
        struct dp_l3_route *m = &l3->routes[(start + n) & l3->route_mask];
        struct dp_l3_route tmp;
        if (!m->used) break;
        tmp = *m;
        memset(m, 0, sizeof(*m));
        l3->route_count--;
        dp_l3_route_add(l3, tmp.prefix, tmp.prefix_len, tmp.nexthop, tmp.egress);
    }
    len_bitmap_rebuild(l3);
    return DP_L3_OK;
}

/* ---- neighbours ------------------------------------------------------- */

static struct dp_l3_neigh *neigh_slot(struct dp_l3 *l3, uint32_t ip, int for_insert)
{
    uint32_t i = mix(ip) & l3->neigh_mask;
    uint32_t n;
    for (n = 0; n < l3->neigh_slots; n++) {
        struct dp_l3_neigh *e = &l3->neigh[(i + n) & l3->neigh_mask];
        if (!e->used) return for_insert ? e : NULL;
        if (e->ip == ip) return e;
    }
    return NULL;
}

int dp_l3_neigh_add(struct dp_l3 *l3, uint32_t ip, const uint8_t mac[6])
{
    struct dp_l3_neigh *e;
    if (!l3->neigh) return DP_ERR_STATE;
    e = neigh_slot(l3, ip, 1);
    if (!e) return DP_L3_ERR_FULL;
    if (!e->used && l3->neigh_count + 1 > (l3->neigh_slots * 3) / 4)
        return DP_L3_ERR_FULL;
    if (!e->used) { l3->neigh_count++; e->used = 1; }
    e->ip = ip;
    e->state = DP_NEIGH_REACHABLE;
    e->probes = 0;
    memcpy(e->mac, mac, 6);
    return DP_L3_OK;
}

int dp_l3_neigh_del(struct dp_l3 *l3, uint32_t ip)
{
    struct dp_l3_neigh *e;
    uint32_t start, n;

    if (!l3->neigh) return DP_ERR_STATE;
    e = neigh_slot(l3, ip, 0);
    if (!e || !e->used) return DP_L3_ERR_NONEIGH;

    start = (uint32_t)(e - l3->neigh);
    memset(e, 0, sizeof(*e));
    l3->neigh_count--;
    for (n = 1; n < l3->neigh_slots; n++) {
        struct dp_l3_neigh *m = &l3->neigh[(start + n) & l3->neigh_mask];
        struct dp_l3_neigh tmp;
        if (!m->used) break;
        tmp = *m;
        memset(m, 0, sizeof(*m));
        l3->neigh_count--;
        dp_l3_neigh_add(l3, tmp.ip, tmp.mac);
    }
    return DP_L3_OK;
}

int dp_l3_iface_set_mac(struct dp_l3 *l3, uint16_t egress, const uint8_t mac[6])
{
    if (egress >= DP_L3_MAX_IFACES) return DP_L3_ERR_RANGE;
    memcpy(l3->iface[egress].mac, mac, 6);
    l3->iface[egress].used = 1;
    return DP_L3_OK;
}

/* ---- lookup ----------------------------------------------------------- */

int dp_l3_lookup(struct dp_l3 *l3, uint32_t dst_ip, struct dp_l3_nh *nh)
{
    int len;

    if (!l3->routes) return DP_ERR_STATE;
    l3->stat_lookup++;
    memset(nh, 0, sizeof(*nh));

    /* Longest-prefix match by construction: walk /32 down to /0 and take the
     * first hit, so no sorted order has to be maintained on insert. Empty
     * prefix lengths are skipped via the bitmap, so a table holding only a
     * default route costs one probe rather than thirty-three. */
    for (len = 32; len >= 0; len--) {
        struct dp_l3_route *r;
        struct dp_l3_neigh *e;
        uint32_t key;

        if (!len_bit_test(l3, (uint8_t)len)) continue;
        key = dst_ip & len_to_mask((uint8_t)len);
        r = route_slot(l3, key, (uint8_t)len, 0);
        if (!r || !r->used) continue;

        nh->egress  = r->egress;
        /* nexthop 0 means directly connected: the destination IS the next hop */
        nh->nexthop = r->nexthop ? r->nexthop : dst_ip;

        e = neigh_slot(l3, nh->nexthop, 0);
        if (e && e->used && e->state == DP_NEIGH_REACHABLE) {
            memcpy(nh->mac, e->mac, 6);
            nh->have_mac = 1;
        } else {
            l3->stat_noneigh++;
        }
        l3->stat_hit++;
        return DP_L3_OK;
    }
    l3->stat_noroute++;
    return DP_L3_ERR_NOROUTE;
}

/* ---- rewrite ---------------------------------------------------------- */

/* Offset of the IPv4 header, allowing a single VLAN tag, or 0 if not IPv4.
 * dp_parse() accepts one tag, so this must agree with it or a tagged packet
 * would be routed using fields read from the wrong offset. */
static uint32_t ip_offset(const uint8_t *pkt, uint32_t len)
{
    uint16_t et;
    if (len < DP_ETH_HLEN + DP_IP4_MIN_HLEN) return 0;
    et = (uint16_t)((pkt[12] << 8) | pkt[13]);
    if (et == DP_ETHERTYPE_IPV4) return DP_ETH_HLEN;
    if (et == DP_ETHERTYPE_VLAN) {
        uint16_t inner;
        if (len < DP_ETH_HLEN + 4 + DP_IP4_MIN_HLEN) return 0;
        inner = (uint16_t)((pkt[16] << 8) | pkt[17]);
        if (inner == DP_ETHERTYPE_IPV4) return DP_ETH_HLEN + 4;
    }
    return 0;
}

int dp_l3_rewrite(struct dp_l3 *l3, uint8_t *pkt, uint32_t len,
                  const struct dp_l3_nh *nh)
{
    uint32_t off = ip_offset(pkt, len);
    uint8_t *ip;
    uint32_t ck;

    if (off == 0) return DP_L3_ERR_SHORT;
    if (!nh->have_mac) return DP_L3_ERR_NONEIGH;

    ip = pkt + off;

    /* A packet arriving with TTL 0 or 1 must not be forwarded. Sending Time
     * Exceeded is the caller's business; refusing is ours. */
    if (ip[8] <= 1) { l3->stat_ttl++; return DP_L3_ERR_TTL; }
    ip[8]--;

    /* Incremental checksum, as RFC 1624 and Linux ip_decrease_ttl: TTL is the
     * high byte of the 16-bit word at offset 8, so the one's-complement sum
     * falls by 0x0100 and the stored checksum rises by it. Recomputing the
     * whole header is also correct and slower for no benefit.
     *
     * Bytes are read and written explicitly rather than cast to a uint16_t,
     * because the checksum is network order and this code runs big-endian on
     * the OCTEON and little-endian in the test harness. */
    ck = (uint32_t)((ip[10] << 8) | ip[11]);
    ck += 0x0100u;
    ck += (ck >= 0xFFFFu);              /* fold the carry */
    ip[10] = (uint8_t)((ck >> 8) & 0xFF);
    ip[11] = (uint8_t)(ck & 0xFF);

    /* L2: destination becomes the next hop; source becomes the egress port
     * when its MAC is known. An egress with no MAC configured keeps the old
     * source rather than writing zeros, which would be worse than stale. */
    memcpy(pkt, nh->mac, 6);
    if (nh->egress < DP_L3_MAX_IFACES && l3->iface[nh->egress].used)
        memcpy(pkt + 6, l3->iface[nh->egress].mac, 6);

    return DP_L3_OK;
}

int dp_l3_iface_set_ip(struct dp_l3 *l3, uint16_t egress, uint32_t ip)
{
    if (egress >= DP_L3_MAX_IFACES) return DP_L3_ERR_RANGE;
    l3->iface[egress].ip = ip;
    l3->iface[egress].used = 1;
    return DP_L3_OK;
}

/* Which egress owns this address, or -1. Used to answer ARP for ourselves. */
int dp_l3_iface_by_ip(const struct dp_l3 *l3, uint32_t ip)
{
    uint32_t i;
    if (ip == 0) return -1;
    for (i = 0; i < DP_L3_MAX_IFACES; i++)
        if (l3->iface[i].used && l3->iface[i].ip == ip)
            return (int)i;
    return -1;
}

/* Neighbour lookup for ARP. Returns the entry whatever its state, so a caller
 * can tell "no entry" from "entry, still INCOMPLETE" -- dp_l3_lookup() cannot,
 * because it deliberately reports only REACHABLE entries as resolved. */
struct dp_l3_neigh *dp_l3_neigh_find(struct dp_l3 *l3, uint32_t ip)
{
    struct dp_l3_neigh *e;
    if (!l3->neigh) return NULL;
    e = neigh_slot(l3, ip, 0);
    return (e && e->used) ? e : NULL;
}

/* Create an INCOMPLETE entry with no MAC, to record that a request is out.
 * Distinct from dp_l3_neigh_add(), which means "this address IS resolved". */
int dp_l3_neigh_reserve(struct dp_l3 *l3, uint32_t ip)
{
    struct dp_l3_neigh *e;
    if (!l3->neigh) return DP_ERR_STATE;
    e = neigh_slot(l3, ip, 1);
    if (!e) return DP_L3_ERR_FULL;
    if (!e->used) {
        if (l3->neigh_count + 1 > (l3->neigh_slots * 3) / 4)
            return DP_L3_ERR_FULL;
        l3->neigh_count++;
        e->used = 1;
        e->ip = ip;
        memset(e->mac, 0, 6);
        e->state = DP_NEIGH_INCOMPLETE;
        e->probes = 0;
    }
    return DP_L3_OK;
}
