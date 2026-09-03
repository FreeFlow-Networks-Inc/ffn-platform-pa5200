/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3.h -- FFN dataplane L3: FIB, neighbours, and header rewrite.
 *
 * Architecture-neutral by the same rule as ffn_dp_oct.h: no chip headers, so
 * this builds natively for the test harness and cross-compiles for mips64
 * (OCTEON III) unchanged. The OCTEON III backend (ffn_dp_io_octeon3.c) carries
 * packets; nothing here knows about PKI, SSO or PKO3.
 *
 * Why this exists
 * ---------------
 * The forwarder had no routing at all. dp_classify() returns an egress port
 * that is carried *by the matched policy rule*, which is bump-in-the-wire
 * forwarding: the rule says where the packet goes, and the destination address
 * is never consulted. That is fine for a transparent firewall between two
 * fixed ports and cannot route between subnets.
 *
 * L3 adds the missing step: destination IP -> longest-prefix match -> next hop
 * -> egress port and next-hop MAC, then the usual rewrite (TTL, checksum, MAC
 * addresses).
 *
 * Endianness, which this file is careful about
 * --------------------------------------------
 * ffn_dp_oct.h warns that IPv4 fields in the fastpath tables are network-order
 * BYTES read with ld_be32(), and that a host-endianness ntohl byte-swaps
 * addresses on big-endian targets while looking correct on x86. The OCTEON is
 * big-endian, so that trap is live here.
 *
 * The rule in this module: **addresses in the FIB and neighbour tables are HOST
 * order**, matching struct dp_tuple, which dp_parse() already produced with
 * ld_be32(). Packet-buffer edits (TTL, checksum, MACs) operate on raw bytes and
 * never assume host order. Callers passing addresses must pass host order.
 */
#ifndef FFN_DP_L3_H
#define FFN_DP_L3_H

#include <stdint.h>
#include <stddef.h>

#define DP_L3_MAX_ROUTES   4096u
#define DP_L3_MAX_NEIGH    4096u
#define DP_L3_MAX_IFACES     64u

/* additional return codes, continuing ffn_dp_oct.h's range */
#define DP_L3_OK            0
#define DP_L3_ERR_NOROUTE (-20)   /* no matching prefix                     */
#define DP_L3_ERR_NONEIGH (-21)   /* route found, next-hop MAC unknown      */
#define DP_L3_ERR_TTL     (-22)   /* TTL would reach zero -- must not fwd   */
#define DP_L3_ERR_FULL    (-23)   /* table full                             */
#define DP_L3_ERR_RANGE   (-24)   /* bad prefix length or interface id      */
#define DP_L3_ERR_SHORT   (-25)   /* packet too short to rewrite            */

/* One routing-table entry. Host-order addresses. */
struct dp_l3_route {
    uint32_t prefix;        /* already masked: prefix & mask                */
    uint32_t mask;
    uint32_t nexthop;       /* 0 means "directly connected": use dst itself */
    uint16_t egress;        /* egress port id, as dp_result.egress          */
    uint8_t  prefix_len;
    uint8_t  used;
};

/* IP -> MAC. Host-order IP, MAC as wire bytes. */
/* Neighbour states. INCOMPLETE means a request has gone out and no reply has
 * come back yet -- the distinction matters, because it is what stops the
 * dataplane re-ARPing on every packet of a flow to an unreachable host. */
#define DP_NEIGH_FREE       0
#define DP_NEIGH_INCOMPLETE 1
#define DP_NEIGH_REACHABLE  2

struct dp_l3_neigh {
    uint32_t ip;
    uint8_t  mac[6];
    uint8_t  state;         /* DP_NEIGH_* */
    uint8_t  probes;        /* requests sent since last reply */
    uint32_t last_probe_ms;
    uint8_t  used;
    uint8_t  pad;
};

/* Per-egress-interface source MAC, used as the new L2 source on rewrite. */
struct dp_l3_iface {
    uint8_t mac[6];
    uint32_t ip;            /* our address on this port, host order */
    uint8_t used;
    uint8_t pad;
};

/*
 * Routes are bucketed by prefix length, and lookup walks 32 -> 0 taking the
 * first hit, which IS longest-prefix match by construction rather than by
 * sorting that has to be maintained. Within a bucket the masked prefix is
 * hashed, so a lookup is at most 33 hash probes regardless of table size --
 * bounded work per packet, which is what a dataplane needs. A trie would use
 * less memory; this is chosen because it is easy to verify correct, and being
 * obviously correct matters more here than saving a few kilobytes.
 */
struct dp_l3 {
    struct dp_l3_route *routes;      /* hash slots, power of two            */
    uint32_t            route_slots;
    uint32_t            route_mask;
    uint32_t            route_count;
    /* which prefix lengths are populated, so lookup skips empty buckets:
     * bit N set means at least one /N route exists. */
    uint32_t            len_bitmap[2];   /* /0../31 in [0], /32 in [1] bit0 */

    struct dp_l3_neigh *neigh;
    uint32_t            neigh_slots;
    uint32_t            neigh_mask;
    uint32_t            neigh_count;

    struct dp_l3_iface  iface[DP_L3_MAX_IFACES];

    /* Caller advances this; ARP uses it to throttle retransmission. Kept here
     * rather than passed down so no existing signature has to change. */
    uint32_t now_ms;

    uint64_t stat_lookup, stat_hit, stat_noroute, stat_noneigh, stat_ttl;
    uint64_t stat_arp_rx, stat_arp_learn, stat_arp_reply, stat_arp_request;
};

/* Result of a route lookup. */
struct dp_l3_nh {
    uint32_t nexthop;       /* host order; resolved gateway or the dst      */
    uint16_t egress;
    uint8_t  mac[6];        /* next-hop MAC, valid only when have_mac       */
    uint8_t  have_mac;
};

int  dp_l3_init(struct dp_l3 *l3, uint32_t route_slots, uint32_t neigh_slots);
void dp_l3_fini(struct dp_l3 *l3);
void dp_l3_flush(struct dp_l3 *l3);

int  dp_l3_route_add(struct dp_l3 *l3, uint32_t prefix, uint8_t prefix_len,
                     uint32_t nexthop, uint16_t egress);
int  dp_l3_route_del(struct dp_l3 *l3, uint32_t prefix, uint8_t prefix_len);
int  dp_l3_neigh_add(struct dp_l3 *l3, uint32_t ip, const uint8_t mac[6]);
int  dp_l3_neigh_del(struct dp_l3 *l3, uint32_t ip);
int  dp_l3_iface_set_mac(struct dp_l3 *l3, uint16_t egress, const uint8_t mac[6]);
int  dp_l3_iface_set_ip(struct dp_l3 *l3, uint16_t egress, uint32_t ip);
int  dp_l3_iface_by_ip(const struct dp_l3 *l3, uint32_t ip);
struct dp_l3_neigh *dp_l3_neigh_find(struct dp_l3 *l3, uint32_t ip);
int  dp_l3_neigh_reserve(struct dp_l3 *l3, uint32_t ip);

/* Longest-prefix match. Fills nh; nh->have_mac says whether the neighbour was
 * resolved. Returns DP_L3_OK, or DP_L3_ERR_NOROUTE. A route with no neighbour
 * is still a successful lookup -- the caller decides whether to punt for ARP. */
int  dp_l3_lookup(struct dp_l3 *l3, uint32_t dst_ip, struct dp_l3_nh *nh);

/*
 * Rewrite an IPv4 packet in place for forwarding: decrement TTL, update the
 * header checksum incrementally, and replace the destination and source MACs.
 * Refuses rather than forwarding a packet whose TTL would reach zero.
 */
int  dp_l3_rewrite(struct dp_l3 *l3, uint8_t *pkt, uint32_t len,
                   const struct dp_l3_nh *nh);

#endif /* FFN_DP_L3_H */
