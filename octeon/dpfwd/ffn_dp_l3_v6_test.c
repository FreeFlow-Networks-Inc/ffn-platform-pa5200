/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_v6_test.c -- unit tests for the IPv6 dataplane L3 layer.
 *
 * Runs natively (little-endian) and cross-compiled for mips64 (big-endian),
 * and running BOTH is the point for the same reason the v4 tests say so: the
 * rewrite path touches packet bytes directly.
 *
 * IPv6 should be the *safer* of the two on that front, because addresses here
 * are stored as wire bytes and there is no header checksum -- so if a
 * big-endian run ever disagrees with a little-endian one, something has
 * introduced a byte-order assumption that does not belong, and that is worth
 * catching loudly.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "ffn_dp_l3_v6.h"

static int fails;

#define CHECK(cond, ...) do {                                               \
    if (!(cond)) { fails++;                                                 \
        printf("  FAIL %s:%d: ", __func__, __LINE__);                       \
        printf(__VA_ARGS__); printf("\n"); }                                \
} while (0)

/* 2001:db8:<a>::<b> -- a readable documentation-prefix address. */
static void mkaddr(uint8_t out[16], uint16_t a, uint16_t b)
{
    memset(out, 0, 16);
    out[0] = 0x20; out[1] = 0x01; out[2] = 0x0d; out[3] = 0xb8;
    out[4] = (uint8_t)(a >> 8); out[5] = (uint8_t)a;
    out[14] = (uint8_t)(b >> 8); out[15] = (uint8_t)b;
}

static void test_mask(void)
{
    uint8_t a[16];

    mkaddr(a, 0x1234, 0x5678);

    /* /128 must not change anything. */
    {
        uint8_t b[16];
        memcpy(b, a, 16);
        dp_l3_v6_mask(b, 128);
        CHECK(memcmp(a, b, 16) == 0, "/128 must be a no-op");
    }
    /* /0 must clear everything. */
    {
        uint8_t b[16], zero[16];
        memcpy(b, a, 16); memset(zero, 0, 16);
        dp_l3_v6_mask(b, 0);
        CHECK(memcmp(b, zero, 16) == 0, "/0 must clear the address");
    }
    /* /32 keeps the first four bytes. */
    {
        uint8_t b[16];
        memcpy(b, a, 16);
        dp_l3_v6_mask(b, 32);
        CHECK(b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0d && b[3] == 0xb8,
              "/32 must keep the first 4 bytes");
        CHECK(b[4] == 0 && b[15] == 0, "/32 must clear the rest");
    }
    /* A non-byte-aligned length is where an off-by-one lives: /33 keeps one
     * more BIT, so byte 4 keeps only its top bit. */
    {
        uint8_t b[16];
        memset(b, 0xFF, 16);
        dp_l3_v6_mask(b, 33);
        CHECK(b[3] == 0xFF, "/33: byte 3 fully kept");
        CHECK(b[4] == 0x80, "/33: byte 4 must keep exactly its top bit, got 0x%02x", b[4]);
        CHECK(b[5] == 0x00, "/33: byte 5 cleared");
    }
    /* /1 and /127, the two ends of the bit-fiddling. */
    {
        uint8_t b[16];
        memset(b, 0xFF, 16);
        dp_l3_v6_mask(b, 1);
        CHECK(b[0] == 0x80 && b[1] == 0, "/1");
        memset(b, 0xFF, 16);
        dp_l3_v6_mask(b, 127);
        CHECK(b[15] == 0xFE, "/127 must clear only the last bit, got 0x%02x", b[15]);
    }
}

static void test_lpm(void)
{
    struct dp_l3_v6 v6;
    struct dp_l3_nh6 nh;
    uint8_t p32[16], p48[16], p128[16], dst[16], gw[16], any[16];

    CHECK(dp_l3_v6_init(&v6, 128, 128) == DP_L3_OK, "init");

    mkaddr(p32, 0, 0);            /* 2001:db8::/32   */
    mkaddr(p48, 0x0001, 0);       /* 2001:db8:1::/48 */
    mkaddr(p128, 0x0001, 0x0009); /* a host route    */
    mkaddr(gw, 0x00ff, 0x0001);
    memset(any, 0, 16);

    CHECK(dp_l3_route6_add(&v6, p32, 32, gw, 10) == DP_L3_OK, "add /32");
    CHECK(dp_l3_route6_add(&v6, p48, 48, gw, 20) == DP_L3_OK, "add /48");
    CHECK(dp_l3_route6_add(&v6, p128, 128, gw, 30) == DP_L3_OK, "add /128");
    CHECK(dp_l3_route6_add(&v6, any, 0, gw, 99) == DP_L3_OK, "add default");

    /* The most specific match must win at every level. */
    mkaddr(dst, 0x0001, 0x0009);
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup host");
    CHECK(nh.egress == 30, "/128 must win, got %u", nh.egress);

    mkaddr(dst, 0x0001, 0x0055);
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup /48");
    CHECK(nh.egress == 20, "/48 must win over /32, got %u", nh.egress);

    mkaddr(dst, 0x0002, 0x0055);
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup /32");
    CHECK(nh.egress == 10, "/32 must win over default, got %u", nh.egress);

    /* Something outside 2001:db8::/32 falls to the default. */
    memset(dst, 0, 16); dst[0] = 0x30;
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup default");
    CHECK(nh.egress == 99, "default must match, got %u", nh.egress);

    /* Deleting the /48 must expose the /32 again -- this is what a broken
     * bitmap rebuild or probe chain would get wrong. */
    CHECK(dp_l3_route6_del(&v6, p48, 48) == DP_L3_OK, "del /48");
    mkaddr(dst, 0x0001, 0x0055);
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup after del");
    CHECK(nh.egress == 10, "/32 must be exposed by deleting /48, got %u", nh.egress);

    /* Deleting something absent is an error, not a silent success. */
    CHECK(dp_l3_route6_del(&v6, p48, 48) == DP_L3_ERR_NOROUTE, "double delete");

    /* An unmasked prefix must normalise: adding 2001:db8:1::9/48 is the same
     * route as 2001:db8:1::/48. */
    {
        uint8_t sloppy[16];
        mkaddr(sloppy, 0x0001, 0x0009);
        CHECK(dp_l3_route6_add(&v6, sloppy, 48, gw, 21) == DP_L3_OK, "sloppy add");
        CHECK(dp_l3_route6_del(&v6, p48, 48) == DP_L3_OK,
              "must be findable by its masked form");
    }

    CHECK(dp_l3_route6_add(&v6, p32, 129, gw, 1) == DP_L3_ERR_RANGE, "plen > 128");
    dp_l3_v6_fini(&v6);
}

static void test_neigh_and_connected(void)
{
    struct dp_l3_v6 v6;
    struct dp_l3_nh6 nh;
    uint8_t pfx[16], dst[16], gw[16];
    uint8_t mac[6] = { 0x02, 0x11, 0x22, 0x33, 0x44, 0x55 };

    CHECK(dp_l3_v6_init(&v6, 64, 64) == DP_L3_OK, "init");
    mkaddr(pfx, 0x000a, 0);
    mkaddr(gw, 0x00ff, 0x0001);
    mkaddr(dst, 0x000a, 0x0007);

    /* Directly connected: an all-zero next hop means "use the destination". */
    CHECK(dp_l3_route6_add(&v6, pfx, 48, NULL, 7) == DP_L3_OK, "connected route");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup");
    CHECK(memcmp(nh.nexthop, dst, 16) == 0,
          "all-zero nexthop must resolve to the destination");
    CHECK(!nh.have_mac, "no neighbour yet, so have_mac must be false");

    /* Learning the neighbour resolves it. */
    CHECK(dp_l3_neigh6_add(&v6, dst, mac) == DP_L3_OK, "neigh add");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup again");
    CHECK(nh.have_mac && memcmp(nh.mac, mac, 6) == 0, "MAC must be resolved");

    /* Via a gateway, the neighbour looked up is the GATEWAY, not the dst. */
    CHECK(dp_l3_route6_add(&v6, pfx, 48, gw, 8) == DP_L3_OK, "gateway route");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup via gw");
    CHECK(memcmp(nh.nexthop, gw, 16) == 0, "nexthop must be the gateway");
    CHECK(!nh.have_mac, "gateway MAC is not known yet");
    CHECK(dp_l3_neigh6_add(&v6, gw, mac) == DP_L3_OK, "learn the gateway");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup via gw again");
    CHECK(nh.have_mac, "gateway MAC must now resolve");

    CHECK(dp_l3_neigh6_del(&v6, gw) == DP_L3_OK, "neigh del");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "still routes");
    CHECK(!nh.have_mac, "deleted neighbour must unresolve");

    dp_l3_v6_fini(&v6);
}

/* Build a minimal IPv6 packet: eth + 40-byte header, optional VLAN tag. */
static uint32_t mkpkt(uint8_t *pkt, int vlan, uint8_t hop)
{
    uint32_t off = 14;

    memset(pkt, 0, 128);
    memset(pkt, 0xEE, 6);                 /* dst mac */
    memset(pkt + 6, 0xAA, 6);             /* src mac */
    if (vlan) {
        pkt[12] = 0x81; pkt[13] = 0x00;
        pkt[14] = 0x00; pkt[15] = 0x64;   /* vid 100 */
        pkt[16] = 0x86; pkt[17] = 0xDD;
        off = 18;
    } else {
        pkt[12] = 0x86; pkt[13] = 0xDD;
    }
    pkt[off + 0] = 0x60;                  /* version 6 */
    pkt[off + 4] = 0x00; pkt[off + 5] = 0x08;   /* payload length 8 */
    pkt[off + 6] = 59;                    /* next header: no next header */
    pkt[off + 7] = hop;                   /* hop limit */
    return off;
}

static void test_rewrite(int vlan)
{
    struct dp_l3_v6 v6;
    struct dp_l3_nh6 nh;
    uint8_t pkt[128], before[128];
    uint8_t our[6] = { 0x02, 0x00, 0x00, 0x00, 0x00, 0x01 };
    uint8_t peer[6] = { 0x02, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF };
    uint32_t off;

    CHECK(dp_l3_v6_init(&v6, 32, 32) == DP_L3_OK, "init");
    CHECK(dp_l3_iface6_set_mac(&v6, 5, our) == DP_L3_OK, "iface mac");

    memset(&nh, 0, sizeof(nh));
    nh.egress = 5;
    memcpy(nh.mac, peer, 6);
    nh.have_mac = 1;

    off = mkpkt(pkt, vlan, 64);
    memcpy(before, pkt, sizeof(pkt));
    CHECK(dp_l3_rewrite6(&v6, pkt, 128, &nh) == DP_L3_OK, "rewrite (vlan=%d)", vlan);

    CHECK(pkt[off + 7] == 63, "hop limit must decrement 64 -> 63, got %u", pkt[off + 7]);
    CHECK(memcmp(pkt, peer, 6) == 0, "destination MAC must be the next hop");
    CHECK(memcmp(pkt + 6, our, 6) == 0, "source MAC must be ours");

    /* Nothing else in the IPv6 header may change. Payload length in
     * particular: decrementing byte 8 instead of byte 7 -- the v4 TTL offset --
     * would corrupt it while leaving hop limit alone. */
    CHECK(pkt[off + 4] == before[off + 4] && pkt[off + 5] == before[off + 5],
          "payload length must be untouched (%02x%02x -> %02x%02x)",
          before[off + 4], before[off + 5], pkt[off + 4], pkt[off + 5]);
    CHECK(pkt[off + 0] == before[off + 0], "version/traffic class untouched");
    CHECK(pkt[off + 6] == before[off + 6], "next header untouched");

    /* A VLAN tag must survive intact. */
    if (vlan) {
        CHECK(pkt[12] == 0x81 && pkt[13] == 0x00, "vlan tpid kept");
        CHECK(pkt[15] == 0x64, "vlan id kept");
    }

    /* Hop limit 1 must be refused, not forwarded as 0 or wrapped to 255. */
    off = mkpkt(pkt, vlan, 1);
    CHECK(dp_l3_rewrite6(&v6, pkt, 128, &nh) == DP_L3_ERR_TTL, "hop limit 1 refused");
    CHECK(pkt[off + 7] == 1, "refused packet must be left unmodified");

    /* No MAC means no rewrite. */
    {
        struct dp_l3_nh6 bare;
        memset(&bare, 0, sizeof(bare));
        bare.egress = 5;
        mkpkt(pkt, vlan, 64);
        CHECK(dp_l3_rewrite6(&v6, pkt, 128, &bare) == DP_L3_ERR_NONEIGH,
              "unresolved neighbour must refuse");
    }

    /* A non-IPv6 frame must be refused rather than having byte 7 poked. */
    mkpkt(pkt, vlan, 64);
    pkt[vlan ? 16 : 12] = 0x08; pkt[vlan ? 17 : 13] = 0x00;   /* IPv4 ethertype */
    CHECK(dp_l3_rewrite6(&v6, pkt, 128, &nh) == DP_L3_ERR_SHORT,
          "a non-IPv6 ethertype must be refused");

    /* Truncated frames must be refused, not read past the end. */
    mkpkt(pkt, vlan, 64);
    CHECK(dp_l3_rewrite6(&v6, pkt, 20, &nh) == DP_L3_ERR_SHORT, "short frame refused");

    dp_l3_v6_fini(&v6);
}

static void test_ecmp(void)
{
    struct dp_l3_v6 v6;
    struct dp_l3_nh6 nh;
    uint8_t pfx[16], dst[16], gw[16];
    uint8_t nhs[3][16];
    uint16_t egr[3] = { 11, 22, 33 };
    int g;

    CHECK(dp_l3_v6_init(&v6, 64, 64) == DP_L3_OK, "init");
    mkaddr(pfx, 0x0020, 0);
    mkaddr(dst, 0x0020, 0x0005);
    mkaddr(gw, 0x00ff, 0x0001);
    memset(nhs, 0, sizeof(nhs));

    g = dp_l3_v6_ecmp_add(&v6, nhs, egr, 3);
    CHECK(g > 0, "group id positive, got %d", g);
    CHECK(dp_l3_route6_add_ecmp(&v6, pfx, 48, (uint16_t)g) == DP_L3_OK, "ecmp route");

    /* Determinism. */
    {
        struct dp_l3_nh6 a, b;
        int i;
        CHECK(dp_l3_lookup6_hash(&v6, dst, 0xABCDu, &a) == DP_L3_OK, "lookup");
        for (i = 0; i < 16; i++) {
            CHECK(dp_l3_lookup6_hash(&v6, dst, 0xABCDu, &b) == DP_L3_OK, "again");
            CHECK(a.egress == b.egress, "same hash must pick the same member");
        }
    }
    /* Every member reachable. */
    {
        int seen[3] = { 0, 0, 0 }, distinct = 0, i;
        uint32_t h;
        for (h = 0; h < 512; h++) {
            CHECK(dp_l3_lookup6_hash(&v6, dst, h, &nh) == DP_L3_OK, "sweep");
            for (i = 0; i < 3; i++) if (nh.egress == egr[i]) seen[i] = 1;
        }
        for (i = 0; i < 3; i++) distinct += seen[i];
        CHECK(distinct == 3, "all 3 members selectable, saw %d", distinct);
    }
    /* Overwriting with a single next hop clears the group. */
    CHECK(dp_l3_route6_add(&v6, pfx, 48, gw, 44) == DP_L3_OK, "overwrite");
    CHECK(dp_l3_lookup6_hash(&v6, dst, 0x1u, &nh) == DP_L3_OK, "after overwrite");
    CHECK(nh.egress == 44, "single next hop must take over, got %u", nh.egress);
    CHECK(memcmp(nh.nexthop, gw, 16) == 0, "and its gateway");

    CHECK(dp_l3_v6_ecmp_add(&v6, nhs, egr, 0) == DP_L3_ERR_RANGE, "n=0");
    CHECK(dp_l3_v6_ecmp_add(&v6, nhs, egr, DP_L3_ECMP_MAX + 1) == DP_L3_ERR_RANGE,
          "n too big");
    CHECK(dp_l3_route6_add_ecmp(&v6, pfx, 48, 0) == DP_L3_ERR_RANGE, "group 0");

    dp_l3_v6_fini(&v6);
}

static void test_capacity(void)
{
    struct dp_l3_v6 v6;
    uint8_t p[16];
    int rc, added = 0;
    unsigned i;

    /* A full table must refuse cleanly rather than corrupt or loop. */
    CHECK(dp_l3_v6_init(&v6, 64, 64) == DP_L3_OK, "init");
    for (i = 0; i < 200u; i++) {
        mkaddr(p, (uint16_t)i, 0);
        rc = dp_l3_route6_add(&v6, p, 48, NULL, 1);
        if (rc == DP_L3_OK) added++;
        else { CHECK(rc == DP_L3_ERR_FULL, "must fail with FULL, got %d", rc); break; }
    }
    CHECK(added > 0 && (uint32_t)added <= v6.route_slots,
          "added %d into %u slots", added, v6.route_slots);
    CHECK(v6.route_count == (uint32_t)added, "count must track inserts");
    dp_l3_v6_fini(&v6);
}

int main(void)
{
    printf("ffn_dp_l3_v6_test (%s-endian)\n",
           (*(const uint16_t *)"\x01\x02" == 0x0102) ? "big" : "little");

    test_mask();
    test_lpm();
    test_neigh_and_connected();
    test_rewrite(0);
    test_rewrite(1);          /* again with a VLAN tag in the way */
    test_ecmp();
    test_capacity();

    if (fails == 0) printf("PASS: all IPv6 L3 tests\n");
    else            printf("FAIL: %d check(s)\n", fails);
    return fails ? 1 : 0;
}
