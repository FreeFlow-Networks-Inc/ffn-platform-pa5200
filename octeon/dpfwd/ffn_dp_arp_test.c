/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_arp_test.c -- unit tests for dataplane ARP.
 *
 * Runs natively and cross-compiled for mips64. The frame checks are
 * byte-level on purpose: ARP is all 16- and 32-bit fields in network order,
 * and code written with htons/ntohs tends to be correct on exactly one of the
 * two machines this has to run on. Comparing raw bytes is endianness-proof.
 */
#include <stdio.h>
#include <string.h>

#include "ffn_dp_arp.h"

static int fails;

#define CHECK(cond, ...) do {                                               \
    if (!(cond)) { fails++;                                                 \
        printf("  FAIL %s:%d: ", __func__, __LINE__);                       \
        printf(__VA_ARGS__); printf("\n"); }                                \
} while (0)

static uint32_t ip4(uint8_t a, uint8_t b, uint8_t c, uint8_t d)
{
    return ((uint32_t)a << 24) | ((uint32_t)b << 16) |
           ((uint32_t)c << 8)  | (uint32_t)d;
}

/* Build an ARP frame. oper 1 = request, 2 = reply. */
static void build_arp(uint8_t *p, uint16_t oper,
                      const uint8_t *smac, uint32_t sip,
                      const uint8_t *tmac, uint32_t tip)
{
    memset(p, 0, 42);
    memset(p + 0, 0xff, 6);
    memcpy(p + 6, smac, 6);
    p[12] = 0x08; p[13] = 0x06;
    p[14] = 0x00; p[15] = 0x01;
    p[16] = 0x08; p[17] = 0x00;
    p[18] = 6; p[19] = 4;
    p[20] = (uint8_t)(oper >> 8); p[21] = (uint8_t)oper;
    memcpy(p + 22, smac, 6);
    p[28] = (uint8_t)(sip >> 24); p[29] = (uint8_t)(sip >> 16);
    p[30] = (uint8_t)(sip >> 8);  p[31] = (uint8_t)sip;
    if (tmac) memcpy(p + 32, tmac, 6);
    p[38] = (uint8_t)(tip >> 24); p[39] = (uint8_t)(tip >> 16);
    p[40] = (uint8_t)(tip >> 8);  p[41] = (uint8_t)tip;
}

static uint8_t ourmac[6]   = { 0x02, 0x00, 0x00, 0x00, 0x00, 0x01 };
static uint8_t peermac[6]  = { 0x02, 0xde, 0xad, 0xbe, 0xef, 0x01 };
static uint8_t stranger[6] = { 0x02, 0x11, 0x11, 0x11, 0x11, 0x11 };

static void setup(struct dp_l3 *l3)
{
    CHECK(dp_l3_init(l3, 64, 64) == DP_L3_OK, "init");
    dp_l3_iface_set_mac(l3, 3, ourmac);
    dp_l3_iface_set_ip(l3, 3, ip4(10,1,0,1));
    dp_l3_route_add(l3, ip4(10,1,0,0), 24, 0, 3);
}

/* A request for one of our addresses must be answered, and answering must
 * teach us who asked. */
static void test_reply(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t pkt[64], out[64];
    uint32_t olen = sizeof(out);

    setup(&l3);
    build_arp(pkt, 1, peermac, ip4(10,1,0,2), NULL, ip4(10,1,0,1));

    CHECK(dp_arp_input(&l3, pkt, 42, out, &olen, NULL) == DP_ARP_TX_REPLY,
          "request for our IP must produce a reply");
    CHECK(olen == 42, "reply must be 42 bytes, got %u", olen);
    CHECK(out[12] == 0x08 && out[13] == 0x06, "ethertype must be ARP");
    CHECK(out[20] == 0x00 && out[21] == 0x02, "oper must be REPLY");
    CHECK(memcmp(out + 0, peermac, 6) == 0, "reply is unicast to the asker");
    CHECK(memcmp(out + 6, ourmac, 6) == 0, "reply source is our port MAC");
    CHECK(memcmp(out + 22, ourmac, 6) == 0, "sender hw addr is ours");
    CHECK(out[28] == 10 && out[29] == 1 && out[30] == 0 && out[31] == 1,
          "sender proto addr wrong: %u.%u.%u.%u",
          out[28], out[29], out[30], out[31]);
    CHECK(memcmp(out + 32, peermac, 6) == 0, "target hw addr is theirs");
    CHECK(out[38] == 10 && out[41] == 2, "target proto addr is theirs");

    CHECK(dp_l3_lookup(&l3, ip4(10,1,0,2), &nh) == DP_L3_OK && nh.have_mac &&
          memcmp(nh.mac, peermac, 6) == 0, "answering should also learn");
    dp_l3_fini(&l3);
}

/* RFC 826: update an entry we already hold for any ARP we see, but only
 * CREATE one when the packet was addressed to us. Learning from every
 * broadcast would let any host on the segment fill the table -- on a firewall
 * that is a cache-poisoning primitive, not just waste. */
static void test_rfc826_learning(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t pkt[64], out[64];
    uint32_t olen;
    uint8_t newmac[6] = { 0x02, 0xde, 0xad, 0xbe, 0xef, 0x99 };
    uint32_t peer = ip4(10,1,0,2), other = ip4(10,1,0,77);

    setup(&l3);
    /* learn the peer legitimately, by answering it */
    build_arp(pkt, 1, peermac, peer, NULL, ip4(10,1,0,1));
    olen = sizeof(out); dp_arp_input(&l3, pkt, 42, out, &olen, NULL);

    /* traffic between two other hosts must not create an entry */
    build_arp(pkt, 1, stranger, other, NULL, ip4(10,1,0,99));
    olen = sizeof(out);
    CHECK(dp_arp_input(&l3, pkt, 42, out, &olen, NULL) == DP_ARP_CONSUMED,
          "a request for someone else must not be answered");
    CHECK(dp_l3_neigh_find(&l3, other) == NULL,
          "must not learn a new neighbour from ARP not addressed to us");

    /* but an entry we already have is refreshed by any ARP */
    build_arp(pkt, 1, newmac, peer, NULL, ip4(10,1,0,50));
    olen = sizeof(out); dp_arp_input(&l3, pkt, 42, out, &olen, NULL);
    CHECK(dp_l3_lookup(&l3, peer, &nh) == DP_L3_OK &&
          memcmp(nh.mac, newmac, 6) == 0,
          "an existing entry should be refreshed by any ARP");
    dp_l3_fini(&l3);
}

static void test_learn_from_reply(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t pkt[64], out[64];
    uint32_t olen = sizeof(out);
    uint32_t gw = ip4(10,1,0,254);
    uint8_t gwmac[6] = { 0x02, 0x99, 0x99, 0x99, 0x99, 0x99 };

    setup(&l3);
    CHECK(dp_l3_neigh_reserve(&l3, gw) == DP_L3_OK, "reserve");
    CHECK(dp_l3_lookup(&l3, gw, &nh) == DP_L3_OK && !nh.have_mac,
          "an INCOMPLETE entry must not look resolved");

    build_arp(pkt, 2, gwmac, gw, ourmac, ip4(10,1,0,1));
    CHECK(dp_arp_input(&l3, pkt, 42, out, &olen, NULL) == DP_ARP_CONSUMED,
          "a reply needs no response");
    CHECK(dp_l3_lookup(&l3, gw, &nh) == DP_L3_OK && nh.have_mac &&
          memcmp(nh.mac, gwmac, 6) == 0, "the reply must resolve the entry");
    dp_l3_fini(&l3);
}

static void test_request_and_rate_limit(void)
{
    struct dp_l3 l3;
    uint8_t out[64];
    uint32_t olen;
    uint32_t tgt = ip4(10,1,0,123);

    setup(&l3);
    l3.now_ms = 10000;

    olen = sizeof(out);
    CHECK(dp_arp_request(&l3, 3, tgt, out, &olen) == DP_ARP_TX_REQUEST,
          "first miss should emit a request");
    CHECK(olen == 42, "request must be 42 bytes");
    CHECK(out[20] == 0x00 && out[21] == 0x01, "oper must be REQUEST");
    CHECK(memcmp(out + 0, "\xff\xff\xff\xff\xff\xff", 6) == 0,
          "requests are broadcast");
    CHECK(memcmp(out + 6, ourmac, 6) == 0, "request source is our port MAC");
    CHECK(out[28] == 10 && out[31] == 1, "sender proto addr is our port IP");
    CHECK(out[38] == 10 && out[41] == 123, "target proto addr is the target");
    CHECK(memcmp(out + 32, "\0\0\0\0\0\0", 6) == 0,
          "target hw addr must be zero -- it is what we are asking for");

    /* The check that matters: without rate limiting, one flow toward an
     * unreachable host becomes an ARP flood at line rate. */
    olen = sizeof(out);
    CHECK(dp_arp_request(&l3, 3, tgt, out, &olen) == DP_ARP_SUPPRESSED,
          "a second request inside the retry window must be suppressed");

    l3.now_ms += DP_ARP_RETRY_MS + 1;
    olen = sizeof(out);
    CHECK(dp_arp_request(&l3, 3, tgt, out, &olen) == DP_ARP_TX_REQUEST,
          "after the retry window another probe should go");

    /* nothing to ask about an address we already know */
    {
        uint8_t m[6] = { 0x02, 1, 2, 3, 4, 5 };
        dp_l3_neigh_add(&l3, ip4(10,1,0,9), m);
        olen = sizeof(out);
        CHECK(dp_arp_request(&l3, 3, ip4(10,1,0,9), out, &olen)
              == DP_ARP_SUPPRESSED, "no request for a reachable neighbour");
    }

    /* an egress with no configuration cannot source a request */
    olen = sizeof(out);
    CHECK(dp_arp_request(&l3, 41, tgt, out, &olen) == DP_ARP_NO_IFACE,
          "unconfigured egress must be refused");
    dp_l3_fini(&l3);
}

/* A host that never answers must not occupy the table for ever, and reaping
 * it must not disturb neighbours that are alive. */
static void test_ageing(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t out[64];
    uint32_t olen;
    uint32_t dead = ip4(10,1,0,200);
    uint8_t livemac[6] = { 0x02, 7, 7, 7, 7, 7 };
    unsigned k;

    setup(&l3);
    dp_l3_neigh_add(&l3, ip4(10,1,0,8), livemac);
    l3.now_ms = 100000;

    for (k = 0; k < DP_ARP_MAX_PROBES + 2; k++) {
        olen = sizeof(out);
        dp_arp_request(&l3, 3, dead, out, &olen);
        l3.now_ms += DP_ARP_RETRY_MS + 1;
    }
    CHECK(dp_l3_neigh_find(&l3, dead) != NULL, "still pending before timeout");
    CHECK(dp_arp_tick(&l3) == 0, "nothing should be reaped before the timeout");

    l3.now_ms += DP_ARP_DEAD_MS + 1;
    CHECK(dp_arp_tick(&l3) >= 1, "tick should reap the dead entry");
    CHECK(dp_l3_neigh_find(&l3, dead) == NULL, "dead entry should be gone");
    CHECK(dp_l3_lookup(&l3, ip4(10,1,0,8), &nh) == DP_L3_OK && nh.have_mac,
          "reaping must not evict a resolved neighbour");
    dp_l3_fini(&l3);
}

static void test_malformed(void)
{
    struct dp_l3 l3;
    uint8_t pkt[64], out[64];
    uint32_t olen;

    setup(&l3);

    memset(pkt, 0, sizeof(pkt));
    pkt[12] = 0x08; pkt[13] = 0x00;              /* IPv4, not ARP */
    olen = sizeof(out);
    CHECK(dp_arp_input(&l3, pkt, 42, out, &olen, NULL) == DP_ARP_NOT_ARP,
          "IPv4 must not be treated as ARP");

    build_arp(pkt, 1, peermac, ip4(10,1,0,2), NULL, ip4(10,1,0,1));
    olen = sizeof(out);
    CHECK(dp_arp_input(&l3, pkt, 20, out, &olen, NULL) == DP_ARP_NOT_ARP,
          "a truncated frame must be refused, not parsed");

    build_arp(pkt, 1, peermac, ip4(10,1,0,2), NULL, ip4(10,1,0,1));
    pkt[16] = 0x86; pkt[17] = 0xdd;              /* ptype IPv6 */
    olen = sizeof(out);
    CHECK(dp_arp_input(&l3, pkt, 42, out, &olen, NULL) == DP_ARP_NOT_ARP,
          "non-IPv4 ARP must be refused rather than mis-parsed");

    build_arp(pkt, 1, peermac, ip4(10,1,0,2), NULL, ip4(10,1,0,1));
    pkt[18] = 8;                                  /* hlen 8, not 6 */
    olen = sizeof(out);
    CHECK(dp_arp_input(&l3, pkt, 42, out, &olen, NULL) == DP_ARP_NOT_ARP,
          "a bad hardware address length must be refused");
    dp_l3_fini(&l3);
}

int main(void)
{
    printf("ffn_dp_arp_test (%s-endian)\n",
           (*(const uint16_t *)"\x01\x02" == 0x0102) ? "big" : "little");

    test_reply();
    test_rfc826_learning();
    test_learn_from_reply();
    test_request_and_rate_limit();
    test_ageing();
    test_malformed();

    if (fails == 0) printf("PASS: all ARP tests\n");
    else            printf("FAIL: %d check(s)\n", fails);
    return fails ? 1 : 0;
}
