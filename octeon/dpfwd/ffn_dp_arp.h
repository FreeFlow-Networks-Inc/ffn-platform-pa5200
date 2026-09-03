/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_arp.h -- ARP for the dataplane router.
 *
 * The FIB punts unresolved next hops expecting something to resolve them. On
 * this box the DP *is* the router -- there is no stack above it -- so this is
 * that something.
 *
 * The caller owns transmission: these functions build a frame and say where to
 * send it, they never touch the wire. That keeps the module architecture-
 * neutral, so it tests natively and cross-compiles for the OCTEON unchanged.
 *
 * The caller must advance l3->now_ms for the rate limiting to work.
 */
#ifndef FFN_DP_ARP_H
#define FFN_DP_ARP_H

#include "ffn_dp_l3.h"

/* Return codes. Positive means "there is a frame in out to transmit". */
#define DP_ARP_CONSUMED     0    /* handled, nothing to send                 */
#define DP_ARP_TX_REPLY     1    /* out holds a reply; send it back          */
#define DP_ARP_TX_REQUEST   2    /* out holds a request; broadcast it        */
#define DP_ARP_SUPPRESSED   3    /* rate limited or already resolved         */
#define DP_ARP_NOT_ARP    (-40)
#define DP_ARP_NO_IFACE   (-41)

#define DP_ARP_FRAME_LEN   42u
#define DP_ARP_RETRY_MS  1000u   /* between probes for one target            */
#define DP_ARP_MAX_PROBES   3u   /* before giving up on a silent host        */
#define DP_ARP_DEAD_MS   30000u  /* then hold it this long before reaping    */
#define DP_ARP_TICK_MS   1000u   /* how often dp_arp_tick() sweeps the table  */

int dp_arp_is_arp(const uint8_t *pkt, uint32_t len);

/* Process a received ARP frame: learn from it, and answer it if it is a
 * request for one of our interface addresses. */
int dp_arp_input(struct dp_l3 *l3, const uint8_t *pkt, uint32_t len,
                 uint8_t *out, uint32_t *out_len, uint16_t *out_egress);

/* Build a request for an unresolved next hop. Rate limited: returns
 * DP_ARP_SUPPRESSED rather than emitting one request per packet. */
int dp_arp_request(struct dp_l3 *l3, uint16_t egress, uint32_t target_ip,
                   uint8_t *out, uint32_t *out_len);

/* Reap unresolved entries that never answered. Returns how many went. */
uint32_t dp_arp_tick(struct dp_l3 *l3);

#endif /* FFN_DP_ARP_H */
