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
#include "ffn_dp_l3_v6_config.h"
#include "ffn_dp_l3_parse.h"

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

/* ---- text -> address -------------------------------------------------- */

/* IPv6 text parsing is where a config layer quietly goes wrong: every case
 * below is a way a hand-edited dp.env actually malforms, and each must be
 * REJECTED rather than silently becoming some other address. A parser that
 * accepts "1:2:3:4:5:6:7:8:9" by ignoring the tail installs a route to
 * somewhere nobody asked for. */
static void test_parse_ipv6(void)
{
    static const struct { const char *txt; const char *hex; } good[] = {
        { "::",                    "00000000000000000000000000000000" },
        { "::1",                   "00000000000000000000000000000001" },
        { "1::",                   "00010000000000000000000000000000" },
        { "2001:db8::1",           "20010db8000000000000000000000001" },
        { "2001:0db8:0000:0000:0000:0000:0000:0001",
                                   "20010db8000000000000000000000001" },
        { "1:2:3:4:5:6:7:8",       "00010002000300040005000600070008" },
        { "::ffff:192.0.2.1",      "00000000000000000000ffffc0000201" },
        { "2001:db8::c0a8:1",      "20010db80000000000000000c0a80001" },
        { "fe80::1",               "fe800000000000000000000000000001" },
        { "0:0:0:0:0:0:0:0",       "00000000000000000000000000000000" },
    };
    static const char *bad[] = {
        "",            ":",           ":1",          "1:",
        "1:::2",       "1::2::3",     "12345::",     "1:2:3:4:5:6:7:8:9",
        "1:2:3:4:5:6:7",              "192.0.2.1",   "::1.2.3.4.5",
        "1.2.3.4::",   "g::1",        "2001:db8::1:", "::ffff:192.0.2.256",
        "1::2:",       "::-1",
    };
    uint8_t a[16], want[16];
    size_t i;
    int j;

    for (i = 0; i < sizeof(good) / sizeof(good[0]); i++) {
        CHECK(dp_l3_parse_ipv6(good[i].txt, a) == 0,
              "must accept %s", good[i].txt);
        for (j = 0; j < 16; j++) {
            int hi = good[i].hex[j * 2], lo = good[i].hex[j * 2 + 1];
            hi = (hi <= '9') ? hi - '0' : (hi | 0x20) - 'a' + 10;
            lo = (lo <= '9') ? lo - '0' : (lo | 0x20) - 'a' + 10;
            want[j] = (uint8_t)((hi << 4) | lo);
        }
        CHECK(memcmp(a, want, 16) == 0, "%s parsed to the wrong bytes",
              good[i].txt);
    }
    for (i = 0; i < sizeof(bad) / sizeof(bad[0]); i++)
        CHECK(dp_l3_parse_ipv6(bad[i], a) != 0, "must reject \"%s\"", bad[i]);

    /* Compressed and expanded spellings of one address must be identical. */
    {
        uint8_t c[16], e[16];
        CHECK(dp_l3_parse_ipv6("2001:db8:0:0:0:0:0:5", e) == 0, "expanded");
        CHECK(dp_l3_parse_ipv6("2001:db8::5", c) == 0, "compressed");
        CHECK(memcmp(c, e, 16) == 0, "spellings must agree");
    }

    /* Prefix form. */
    {
        uint8_t p[16];
        uint8_t plen = 0;
        CHECK(dp_l3_parse_prefix6("2001:db8::/32", p, &plen) == 0, "prefix");
        CHECK(plen == 32, "plen 32, got %u", plen);
        CHECK(dp_l3_parse_prefix6("::/0", p, &plen) == 0, "default route");
        CHECK(plen == 0, "plen 0, got %u", plen);
        CHECK(dp_l3_parse_prefix6("2001:db8::/128", p, &plen) == 0, "host route");
        CHECK(dp_l3_parse_prefix6("2001:db8::/129", p, &plen) != 0, "reject /129");
        CHECK(dp_l3_parse_prefix6("2001:db8::", p, &plen) != 0, "reject no slash");
        CHECK(dp_l3_parse_prefix6("2001:db8::/", p, &plen) != 0, "reject empty len");
        CHECK(dp_l3_parse_prefix6("2001:db8::/1x", p, &plen) != 0, "reject junk len");
    }

    /* The DP addresses interfaces by INDEX. A Linux name is well-formed on the
     * MP and a syntax error here, which is exactly the trap the renderer
     * portmap exists to prevent. */
    {
        uint16_t d = 0;
        CHECK(dp_l3_parse_dev("3", &d) == 0 && d == 3, "dev 3");
        CHECK(dp_l3_parse_dev("0", &d) == 0 && d == 0, "dev 0 is valid");
        CHECK(dp_l3_parse_dev("65535", &d) == 0, "dev 65535");
        CHECK(dp_l3_parse_dev("65536", &d) != 0, "reject out of range");
        CHECK(dp_l3_parse_dev("enp11s0f0", &d) != 0, "reject a Linux name");
        CHECK(dp_l3_parse_dev("3x", &d) != 0, "reject trailing junk");
        CHECK(dp_l3_parse_dev("-1", &d) != 0, "reject negative");
        CHECK(dp_l3_parse_dev(" 3", &d) != 0, "reject leading space");
    }
}

/* ---- config -> FIB ----------------------------------------------------- */

static void test_config(void)
{
    struct dp_l3_config_stats st;
    struct dp_l3_v6 v6;
    struct dp_l3_nh6 nh;
    uint8_t dst[16];
    size_t i;

    static const char *badroute[] = {
        "2001:db8::/32",                      /* no dev at all            */
        "2001:db8::/129 dev 1",               /* prefix too long          */
        "2001:db8::/32 dev enp11s0f0",        /* a name, not an index     */
        "2001:db8::/32 via nonsense dev 1",   /* unparseable gateway      */
        "2001:db8::/32 wat 1 dev 1",          /* unknown keyword          */
        "not-an-address dev 1",               /* not an address at all    */
    };

    memset(&st, 0, sizeof(st));
    CHECK(dp_l3_v6_init(&v6, 64, 64) == DP_L3_OK, "init");

    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.1", "2001:db8::/32 dev 3", &st)
          == DP_L3_CFG_OK, "connected route");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.2",
                               "::/0 via 2001:db8::1 dev 1", &st)
          == DP_L3_CFG_OK, "default via a gateway");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.neigh6.1",
                               "2001:db8::1 lladdr 02:aa:bb:cc:dd:01", &st)
          == DP_L3_CFG_OK, "neighbour");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.iface6.3.mac",
                               "02:39:f1:cb:45:00", &st)
          == DP_L3_CFG_OK, "iface mac");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.iface6.3.ip", "2001:db8:3::1", &st)
          == DP_L3_CFG_OK, "iface ip");
    CHECK(st.routes == 2 && st.neigh == 1 && st.ifaces == 2 && st.errors == 0,
          "routes=%u neigh=%u ifaces=%u errors=%u",
          st.routes, st.neigh, st.ifaces, st.errors);

    /* Keys belonging to somebody else are SKIPPED, not errors. The relayed
     * file is the whole config for this node; a DP that refused to start
     * because the MP added a knob it had not heard of would be worse than one
     * that ignores it. The v4 L3 keys are the ones that matter here: they are
     * one character away from ours and must not be mistaken for v6. */
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route.1", "10.1.0.0/16 dev 3", &st)
          == DP_L3_CFG_SKIP, "a v4 route key is not ours");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.neigh.1",
                               "10.0.0.1 lladdr 02:aa:bb:cc:dd:ee", &st)
          == DP_L3_CFG_SKIP, "a v4 neigh key is not ours");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.iface.3.mac", "02:39:f1:cb:45:00", &st)
          == DP_L3_CFG_SKIP, "a v4 iface key is not ours");
    CHECK(dp_l3_v6_config_line(&v6, "all.platform", "pa5220", &st)
          == DP_L3_CFG_SKIP, "all.* is not ours");
    CHECK(st.errors == 0, "skipping must not raise errors, got %u", st.errors);

    /* The routes actually went in, and resolve. */
    CHECK(dp_l3_parse_ipv6("2001:db8:5::9", dst) == 0, "dst");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "lookup the /32");
    CHECK(nh.egress == 3, "egress 3, got %u", nh.egress);

    CHECK(dp_l3_parse_ipv6("2400::1", dst) == 0, "off-prefix dst");
    CHECK(dp_l3_lookup6(&v6, dst, &nh) == DP_L3_OK, "falls to the default");
    CHECK(nh.egress == 1, "egress 1, got %u", nh.egress);
    CHECK(nh.have_mac, "the gateway neighbour was configured");
    CHECK(nh.mac[0] == 0x02 && nh.mac[5] == 0x01, "gateway mac resolved");

    /* Every malformed route is an error and adds nothing. */
    {
        uint32_t before = st.routes;
        for (i = 0; i < sizeof(badroute) / sizeof(badroute[0]); i++)
            CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.9", badroute[i], &st)
                  < 0, "must reject \"%s\"", badroute[i]);
        CHECK(st.routes == before, "a rejected route must not be installed");
        CHECK(st.errors == sizeof(badroute) / sizeof(badroute[0]),
              "every rejection counted: %u", st.errors);
    }

    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.neigh6.9",
                               "2001:db8::2 lladdr 02:aa", &st)
          < 0, "short mac rejected");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.neigh6.9",
                               "2001:db8::2 02:aa:bb:cc:dd:ee", &st)
          < 0, "missing lladdr keyword rejected");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.iface6.3.wat",
                               "02:aa:bb:cc:dd:ee", &st)
          < 0, "unknown iface attribute rejected");
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.iface6.x.mac",
                               "02:aa:bb:cc:dd:ee", &st)
          < 0, "non-numeric egress rejected");

    dp_l3_v6_fini(&v6);
}

static void test_config_ecmp(void)
{
    struct dp_l3_config_stats st;
    struct dp_l3_v6 v6;
    struct dp_l3_nh6 nh;
    uint8_t dst[16];
    int seen[2], h, distinct;

    memset(&st, 0, sizeof(st));
    CHECK(dp_l3_v6_init(&v6, 64, 64) == DP_L3_OK, "init");

    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.1",
              "2001:db8::/32 nexthop via 2001:db8::2 dev 11"
              " nexthop via 2001:db8::3 dev 22", &st)
          == DP_L3_CFG_OK, "two-leg multipath");
    CHECK(st.routes == 1 && st.errors == 0, "routes=%u errors=%u",
          st.routes, st.errors);

    CHECK(dp_l3_parse_ipv6("2001:db8:7::9", dst) == 0, "dst");
    seen[0] = seen[1] = 0;
    for (h = 0; h < 512; h++) {
        CHECK(dp_l3_lookup6_hash(&v6, dst, (uint32_t)h, &nh) == DP_L3_OK, "sweep");
        if (nh.egress == 11) seen[0] = 1;
        else if (nh.egress == 22) seen[1] = 1;
        else CHECK(0, "unexpected egress %u", nh.egress);
    }
    distinct = seen[0] + seen[1];
    CHECK(distinct == 2, "both legs must be reachable, saw %d", distinct);

    /* A leg with no dev is a syntax error, and NOTHING may be installed --
     * a half-built group holding the legs that happened to parse first is
     * worse than a rejected line, because it forwards. */
    {
        uint32_t before = st.routes;
        CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.2",
                  "2001:db8:2::/48 nexthop via 2001:db8::2 dev 11"
                  " nexthop via 2001:db8::3", &st) < 0, "leg without dev");
        CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.3",
                  "2001:db8:3::/48 nexthop via bogus dev 11"
                  " nexthop via 2001:db8::3 dev 22", &st) < 0, "bad gateway");
        CHECK(st.routes == before, "nothing installed from a rejected multipath");
    }

    /* Exactly DP_L3_ECMP_MAX legs is the largest LEGAL multipath and must be
     * accepted. This is the case that catches a token budget sized to the
     * maximum instead of past it: with too small a budget the line is cut
     * mid-leg and rejected as malformed, so "too many legs is rejected" passes
     * while every large legal route is broken. */
    {
        char ok[DP_L3_CFG_MAX_VALUE];
        int i, off = 0;
        off += sprintf(ok + off, "2001:db8:a::/48");
        for (i = 0; i < (int)DP_L3_ECMP_MAX; i++)
            off += sprintf(ok + off, " nexthop via 2001:db8::%d dev %d",
                           i + 2, i + 1);
        CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.6", ok, &st)
              == DP_L3_CFG_OK, "DP_L3_ECMP_MAX legs must be ACCEPTED");
    }

    /* More legs than a group can hold is a rejection, not a truncation. */
    {
        char big[DP_L3_CFG_MAX_VALUE];
        int i, off = 0;
        off += sprintf(big + off, "2001:db8:9::/48");
        for (i = 0; i < (int)DP_L3_ECMP_MAX + 1; i++)
            off += sprintf(big + off, " nexthop via 2001:db8::%d dev %d",
                           i + 2, i + 1);
        CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.4", big, &st) < 0,
              "more than DP_L3_ECMP_MAX legs must be rejected");
    }

    /* A single-path route must NOT become a one-member group: that is a
     * different object in the FIB and would change what lookup returns. */
    CHECK(dp_l3_v6_config_line(&v6, "dp.l3.route6.5",
                               "2001:db8:5::/48 via 2001:db8::9 dev 4", &st)
          == DP_L3_CFG_OK, "plain single path still works");

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
    test_parse_ipv6();
    test_config();
    test_config_ecmp();

    if (fails == 0) printf("PASS: all IPv6 L3 tests\n");
    else            printf("FAIL: %d check(s)\n", fails);
    return fails ? 1 : 0;
}
