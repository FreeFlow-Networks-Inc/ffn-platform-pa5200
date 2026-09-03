/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_arp.c -- ARP for the dataplane router.
 *
 * The FIB resolves a next hop to an egress port and, when it can, a MAC. When
 * it cannot, dp_l3_resolve() returns FP_LOCAL_W "punt so the stack can ARP" --
 * except the DP *is* the router, and there is no stack above it to do that.
 * This module is what that punt was waiting for.
 *
 * Three jobs:
 *   - learn neighbours from ARP we receive, so the FIB can resolve next hops
 *   - answer requests for our own interface addresses, so peers can reach us
 *   - generate requests for next hops we could not resolve, rate-limited
 *
 * Everything is byte-oriented on the wire and host-order in the tables, the
 * same rule as ffn_dp_l3.c. No htons/ntohs anywhere: on the big-endian OCTEON
 * those are identity and on x86 they are a swap, so code written with them
 * tends to be right on exactly one of the two machines this must run on.
 *
 * Ethernet ARP frame, 42 bytes:
 *   0  dst mac      6  src mac    12 ethertype 0x0806
 *   14 htype(1)     16 ptype(0x0800)  18 hlen(6)  19 plen(4)  20 oper
 *   22 sender mac   28 sender ip  32 target mac   38 target ip
 */
#include <string.h>

#include "ffn_dp_arp.h"

#define ARP_HDR_OFF     14u
#define ARP_FRAME_LEN   42u
#define ETHERTYPE_ARP   0x0806u
#define ARP_HTYPE_ETH   1u
#define ARP_PTYPE_IPV4  0x0800u
#define ARP_OP_REQUEST  1u
#define ARP_OP_REPLY    2u

static const uint8_t bcast_mac[6] = { 0xff, 0xff, 0xff, 0xff, 0xff, 0xff };

/* ---- byte helpers, explicit so endianness cannot be got wrong ---------- */

static uint16_t rd16(const uint8_t *p) { return (uint16_t)((p[0] << 8) | p[1]); }

static uint32_t rd32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  | (uint32_t)p[3];
}

static void wr16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v;
}

static void wr32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);  p[3] = (uint8_t)v;
}

int dp_arp_is_arp(const uint8_t *pkt, uint32_t len)
{
    if (len < ARP_FRAME_LEN) return 0;
    return rd16(pkt + 12) == ETHERTYPE_ARP;
}

/* ---- learning ---------------------------------------------------------- */

/*
 * RFC 826's rule, and it matters: update an entry we already have for any ARP
 * we see, but only CREATE one if the packet was addressed to us. Learning a
 * new neighbour from every broadcast on the segment would let any host fill
 * the table, and on a firewall that is a cache-poisoning primitive rather than
 * merely wasteful.
 */
static void arp_learn(struct dp_l3 *l3, uint32_t sender_ip,
                      const uint8_t *sender_mac, int addressed_to_us)
{
    struct dp_l3_neigh *e;

    if (sender_ip == 0) return;                 /* ARP probe: nothing to learn */
    if (sender_mac[0] & 0x01) return;           /* multicast source: bogus     */

    e = dp_l3_neigh_find(l3, sender_ip);
    if (!e && !addressed_to_us) return;

    if (dp_l3_neigh_add(l3, sender_ip, sender_mac) == DP_L3_OK)
        l3->stat_arp_learn++;
}

/* ---- receive ----------------------------------------------------------- */

int dp_arp_input(struct dp_l3 *l3, const uint8_t *pkt, uint32_t len,
                 uint8_t *out, uint32_t *out_len, uint16_t *out_egress)
{
    const uint8_t *arp;
    uint16_t oper;
    uint32_t sender_ip, target_ip;
    const uint8_t *sender_mac;
    int ifidx, to_us;

    if (!dp_arp_is_arp(pkt, len)) return DP_ARP_NOT_ARP;
    arp = pkt + ARP_HDR_OFF;

    /* Only Ethernet/IPv4 ARP. Anything else is not ours to interpret, and
     * guessing at the field offsets of an unknown address family would read
     * past the parts we validated. */
    if (rd16(arp + 0) != ARP_HTYPE_ETH || rd16(arp + 2) != ARP_PTYPE_IPV4 ||
        arp[4] != 6 || arp[5] != 4)
        return DP_ARP_NOT_ARP;

    l3->stat_arp_rx++;

    oper       = rd16(arp + 6);
    sender_mac = arp + 8;
    sender_ip  = rd32(arp + 14);
    target_ip  = rd32(arp + 24);

    ifidx = dp_l3_iface_by_ip(l3, target_ip);
    to_us = (ifidx >= 0);

    arp_learn(l3, sender_ip, sender_mac, to_us);

    if (oper != ARP_OP_REQUEST || !to_us)
        return DP_ARP_CONSUMED;      /* learned what we could; nothing to send */

    /* A request for one of our addresses: answer it. */
    if (!out || !out_len || *out_len < ARP_FRAME_LEN) return DP_ARP_CONSUMED;

    memset(out, 0, ARP_FRAME_LEN);
    memcpy(out + 0, sender_mac, 6);                    /* unicast the reply   */
    memcpy(out + 6, l3->iface[ifidx].mac, 6);
    wr16(out + 12, ETHERTYPE_ARP);

    wr16(out + ARP_HDR_OFF + 0, ARP_HTYPE_ETH);
    wr16(out + ARP_HDR_OFF + 2, ARP_PTYPE_IPV4);
    out[ARP_HDR_OFF + 4] = 6;
    out[ARP_HDR_OFF + 5] = 4;
    wr16(out + ARP_HDR_OFF + 6, ARP_OP_REPLY);
    memcpy(out + ARP_HDR_OFF + 8, l3->iface[ifidx].mac, 6);
    wr32(out + ARP_HDR_OFF + 14, target_ip);           /* us                  */
    memcpy(out + ARP_HDR_OFF + 18, sender_mac, 6);
    wr32(out + ARP_HDR_OFF + 24, sender_ip);           /* them                */

    /* The reply leaves by the interface that owns the queried address, which
     * is the only port where that address is meaningful. dp_process cannot
     * work this out -- it is not told the ingress port -- so report it. */
    if (out_egress) *out_egress = (uint16_t)ifidx;
    *out_len = ARP_FRAME_LEN;
    l3->stat_arp_reply++;
    return DP_ARP_TX_REPLY;
}

/* ---- request generation ------------------------------------------------ */

int dp_arp_request(struct dp_l3 *l3, uint16_t egress, uint32_t target_ip,
                   uint8_t *out, uint32_t *out_len)
{
    struct dp_l3_neigh *e;
    uint32_t src_ip;

    if (egress >= DP_L3_MAX_IFACES || !l3->iface[egress].used)
        return DP_ARP_NO_IFACE;
    if (!out || !out_len || *out_len < ARP_FRAME_LEN)
        return DP_ARP_NO_IFACE;

    src_ip = l3->iface[egress].ip;

    /*
     * Rate limit. Without this the dataplane emits one request per PACKET to
     * an unresolved host, so a single flow toward something unreachable turns
     * into an ARP flood at line rate -- the failure mode is worse than the
     * problem it is trying to solve.
     *
     * An INCOMPLETE entry records that a request is outstanding; a REACHABLE
     * one needs no request at all.
     */
    e = dp_l3_neigh_find(l3, target_ip);
    if (e && e->state == DP_NEIGH_REACHABLE)
        return DP_ARP_SUPPRESSED;
    if (e && e->state == DP_NEIGH_INCOMPLETE) {
        uint32_t since = l3->now_ms - e->last_probe_ms;   /* wraps correctly */
        if (since < DP_ARP_RETRY_MS)
            return DP_ARP_SUPPRESSED;
        if (e->probes >= DP_ARP_MAX_PROBES) {
            /* Give up rather than probe a dead host forever. The entry is left
             * INCOMPLETE so the FIB keeps reporting unresolved; ageing it out
             * is dp_arp_tick()'s job. */
            return DP_ARP_SUPPRESSED;
        }
    }

    if (!e) {
        if (dp_l3_neigh_reserve(l3, target_ip) != DP_L3_OK)
            return DP_ARP_SUPPRESSED;      /* table full: drop, do not flood */
        e = dp_l3_neigh_find(l3, target_ip);
        if (!e) return DP_ARP_SUPPRESSED;
    }
    e->state = DP_NEIGH_INCOMPLETE;
    e->last_probe_ms = l3->now_ms;
    if (e->probes < 255) e->probes++;

    memset(out, 0, ARP_FRAME_LEN);
    memcpy(out + 0, bcast_mac, 6);
    memcpy(out + 6, l3->iface[egress].mac, 6);
    wr16(out + 12, ETHERTYPE_ARP);

    wr16(out + ARP_HDR_OFF + 0, ARP_HTYPE_ETH);
    wr16(out + ARP_HDR_OFF + 2, ARP_PTYPE_IPV4);
    out[ARP_HDR_OFF + 4] = 6;
    out[ARP_HDR_OFF + 5] = 4;
    wr16(out + ARP_HDR_OFF + 6, ARP_OP_REQUEST);
    memcpy(out + ARP_HDR_OFF + 8, l3->iface[egress].mac, 6);
    wr32(out + ARP_HDR_OFF + 14, src_ip);
    /* target MAC stays zero in a request -- that is what is being asked */
    wr32(out + ARP_HDR_OFF + 24, target_ip);

    *out_len = ARP_FRAME_LEN;
    l3->stat_arp_request++;
    return DP_ARP_TX_REQUEST;
}

/* ---- ageing ------------------------------------------------------------ */

uint32_t dp_arp_tick(struct dp_l3 *l3)
{
    uint32_t i, dropped = 0;

    /* Reap INCOMPLETE entries that never resolved, so a burst of traffic to
     * dead addresses cannot permanently occupy the table. REACHABLE entries
     * are deliberately left alone here: expiring them would be correct ARP
     * behaviour but would also blackhole live traffic for a probe interval,
     * and revalidation belongs in a separate, gentler pass. */
    for (i = 0; i < l3->neigh_slots; i++) {
        struct dp_l3_neigh *e = &l3->neigh[i];
        if (!e->used || e->state != DP_NEIGH_INCOMPLETE) continue;
        if (e->probes < DP_ARP_MAX_PROBES) continue;
        if ((uint32_t)(l3->now_ms - e->last_probe_ms) < DP_ARP_DEAD_MS) continue;
        dp_l3_neigh_del(l3, e->ip);
        dropped++;
        i--;                    /* the delete reinserts the cluster behind us */
    }
    return dropped;
}
