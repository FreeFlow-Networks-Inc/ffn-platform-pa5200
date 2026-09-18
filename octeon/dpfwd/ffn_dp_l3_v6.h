/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_v6.h -- FFN dataplane L3 for IPv6: FIB, neighbours, rewrite.
 *
 * Architecture-neutral by the same rule as ffn_dp_l3.h: no chip headers, so it
 * builds natively for the harness and cross-compiles for mips64 unchanged.
 *
 * WHY A SEPARATE MODULE AND NOT A WIDENED ffn_dp_l3.c
 * ---------------------------------------------------
 * The v4 path forwards packets on real silicon today. Widening its route
 * struct to 128 bits would touch every lookup, every insert and the rewrite --
 * i.e. it would put the working path at risk to add the new one. A parallel
 * module costs a second table and leaves v4 byte-for-byte as it was.
 *
 * ADDRESSES ARE STORED AS WIRE BYTES HERE. THIS DIFFERS FROM ffn_dp_l3.c ON
 * PURPOSE -- READ THIS BEFORE ADDING A CALLER.
 * ------------------------------------------------------------------------
 * ffn_dp_l3.c keeps IPv4 addresses in HOST order, because dp_parse() already
 * produced them that way with ld_be32() and a uint32_t has an unambiguous host
 * order. A 128-bit address has no such thing: there is no uint128_t to swap,
 * and "host order" for a 16-byte quantity is a question with no useful answer.
 *
 * So IPv6 addresses live here exactly as they appear on the wire, as
 * uint8_t[16]. Consequences, all of them good:
 *
 *   - prefix matching is a byte/bit compare, with no swapping anywhere;
 *   - the same bytes are copied into the packet on rewrite, so there is no
 *     conversion step to get backwards;
 *   - the code is identical on x86 and on the big-endian OCTEON, which is
 *     precisely the bug class ffn_dp_oct.h warns about.
 *
 * The cost is that a caller must NOT pass an address it has byte-swapped.
 * Anything that came off the wire, out of inet_pton(), or out of a text parse
 * is already correct.
 *
 * WHAT IPv6 DOES NOT HAVE, WHICH SIMPLIFIES THE REWRITE
 * -----------------------------------------------------
 * No header checksum. The v4 rewrite has to update it incrementally and the v4
 * tests sweep it, because getting that wrong corrupts every forwarded packet.
 * IPv6 deleted the field, so the rewrite here is: decrement Hop Limit, replace
 * the two MACs. There is deliberately no checksum code to get wrong.
 *
 * Note the field is at a DIFFERENT OFFSET from v4's TTL -- Hop Limit is byte 7
 * of the IPv6 header, TTL is byte 8 of the IPv4 header -- and the IPv6 header
 * is a fixed 40 bytes with no IHL to read. Copying the v4 rewrite and changing
 * the constant would be wrong in both places.
 */
#ifndef FFN_DP_L3_V6_H
#define FFN_DP_L3_V6_H

#include <stdint.h>
#include <stddef.h>

#include "ffn_dp_l3.h"      /* DP_L3_OK / DP_L3_ERR_*, reused verbatim */

#define DP_L3_V6_MAX_ROUTES 4096u
#define DP_L3_V6_MAX_NEIGH  4096u
#define DP_L3_V6_ADDR_LEN     16u

/* One IPv6 routing-table entry. Wire-byte addresses (see the header note). */
struct dp_l3_route6 {
    uint8_t  prefix[DP_L3_V6_ADDR_LEN];   /* already masked to plen         */
    uint8_t  nexthop[DP_L3_V6_ADDR_LEN];  /* all-zero = directly connected  */
    uint16_t egress;
    uint16_t ecmp;                        /* 0 = single next hop            */
    uint8_t  plen;                        /* 0..128                         */
    uint8_t  used;
};

struct dp_l3_neigh6 {
    uint8_t  ip[DP_L3_V6_ADDR_LEN];
    uint8_t  mac[6];
    uint8_t  state;                       /* DP_NEIGH_* from ffn_dp_l3.h    */
    uint8_t  probes;
    uint32_t last_probe_ms;
    uint8_t  used;
};

struct dp_l3_iface6 {
    uint8_t mac[6];
    uint8_t ip[DP_L3_V6_ADDR_LEN];
    uint8_t used;
};

/*
 * Same shape as the v4 table and for the same reason: routes are bucketed by
 * prefix length and lookup walks 128 -> 0 taking the first hit, which IS
 * longest-prefix match by construction rather than by a sort that has to be
 * maintained. The bitmap skips empty lengths, so a table holding only a
 * default route costs one probe instead of 129.
 *
 * 129 possible lengths need 129 bits, hence five uint32_t rather than v4's
 * two. Getting that array size wrong is a silent out-of-bounds write on
 * insert, so it is derived here rather than written as a number: /0../128.
 */
#define DP_L3_V6_LENBITS_WORDS  ((129u + 31u) / 32u)

struct dp_l3_v6 {
    struct dp_l3_route6 *routes;
    uint32_t             route_slots;
    uint32_t             route_mask;
    uint32_t             route_count;
    uint32_t             len_bitmap[DP_L3_V6_LENBITS_WORDS];

    struct dp_l3_neigh6 *neigh;
    uint32_t             neigh_slots;
    uint32_t             neigh_mask;
    uint32_t             neigh_count;

    struct dp_l3_iface6  iface[DP_L3_MAX_IFACES];

    /* ECMP groups, identical scheme to v4: 1-based ids so 0 means "none". */
    struct dp_l3_ecmp    ecmp[DP_L3_MAX_ECMP_GROUPS];
    uint32_t             ecmp_count;

    uint32_t now_ms;

    uint64_t stat_lookup, stat_hit, stat_noroute, stat_noneigh, stat_ttl;
};

/* Result of an IPv6 route lookup. */
struct dp_l3_nh6 {
    uint8_t  nexthop[DP_L3_V6_ADDR_LEN];  /* resolved gateway, or the dst   */
    uint16_t egress;
    uint8_t  mac[6];
    uint8_t  have_mac;
};

int  dp_l3_v6_init(struct dp_l3_v6 *v6, uint32_t route_slots, uint32_t neigh_slots);
void dp_l3_v6_fini(struct dp_l3_v6 *v6);
void dp_l3_v6_flush(struct dp_l3_v6 *v6);

int  dp_l3_route6_add(struct dp_l3_v6 *v6, const uint8_t prefix[16], uint8_t plen,
                      const uint8_t nexthop[16], uint16_t egress);
int  dp_l3_route6_del(struct dp_l3_v6 *v6, const uint8_t prefix[16], uint8_t plen);
int  dp_l3_neigh6_add(struct dp_l3_v6 *v6, const uint8_t ip[16], const uint8_t mac[6]);
int  dp_l3_neigh6_del(struct dp_l3_v6 *v6, const uint8_t ip[16]);
int  dp_l3_iface6_set_mac(struct dp_l3_v6 *v6, uint16_t egress, const uint8_t mac[6]);
int  dp_l3_iface6_set_ip(struct dp_l3_v6 *v6, uint16_t egress, const uint8_t ip[16]);

/* ECMP, same contract as v4: 1-based group ids, deterministic member choice. */
int  dp_l3_v6_ecmp_add(struct dp_l3_v6 *v6, const uint8_t nexthop[][16],
                       const uint16_t *egress, uint8_t n);
int  dp_l3_route6_add_ecmp(struct dp_l3_v6 *v6, const uint8_t prefix[16],
                           uint8_t plen, uint16_t group);

/* Longest-prefix match. A route with no neighbour is still a successful
 * lookup; have_mac says whether the L2 address is known. */
int  dp_l3_lookup6(struct dp_l3_v6 *v6, const uint8_t dst[16],
                   struct dp_l3_nh6 *nh);
int  dp_l3_lookup6_hash(struct dp_l3_v6 *v6, const uint8_t dst[16],
                        uint32_t flow_hash, struct dp_l3_nh6 *nh);

/*
 * Rewrite an IPv6 packet in place: decrement Hop Limit and replace both MACs.
 * There is no checksum to update. Refuses rather than forwarding a packet
 * whose Hop Limit would reach zero.
 *
 * Handles an optional single 802.1Q tag, as the v4 rewrite does.
 */
int  dp_l3_rewrite6(struct dp_l3_v6 *v6, uint8_t *pkt, uint32_t len,
                    const struct dp_l3_nh6 *nh);

/* Mask an address down to a prefix length, in place. Exposed because callers
 * that parse config need the same normalisation the table applies, and two
 * implementations of "mask 16 bytes to N bits" is one too many. */
void dp_l3_v6_mask(uint8_t addr[16], uint8_t plen);

#endif /* FFN_DP_L3_V6_H */
