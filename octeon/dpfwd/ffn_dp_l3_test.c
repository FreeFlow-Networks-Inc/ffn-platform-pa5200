/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_test.c -- unit tests for the dataplane L3 layer.
 *
 * Runs natively (little-endian) and cross-compiled for mips64 (big-endian).
 * Running it on BOTH is the point: the checksum and rewrite paths touch packet
 * bytes directly, and an endianness mistake there passes on x86 and corrupts
 * every forwarded packet on the OCTEON. ffn_dp_oct.h warns about exactly this
 * class of bug, so the checksum is verified by full recomputation rather than
 * by comparing against a constant somebody wrote down.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "ffn_dp_l3.h"
#include "ffn_dp_l3_config.h"

static int fails;

#define CHECK(cond, ...) do {                                               \
    if (!(cond)) { fails++;                                                 \
        printf("  FAIL %s:%d: ", __func__, __LINE__);                       \
        printf(__VA_ARGS__); printf("\n"); }                                \
} while (0)

/* a.b.c.d -> host order, matching what dp_parse() produces */
static uint32_t ip4(uint8_t a, uint8_t b, uint8_t c, uint8_t d)
{
    return ((uint32_t)a << 24) | ((uint32_t)b << 16) |
           ((uint32_t)c << 8)  | (uint32_t)d;
}

/* One's-complement sum over the IPv4 header. A correct header sums to 0xFFFF,
 * which is what makes this an independent check of the incremental update. */
static uint16_t ip_csum(const uint8_t *ip, int hlen)
{
    uint32_t sum = 0;
    int i;
    for (i = 0; i < hlen; i += 2)
        sum += (uint32_t)((ip[i] << 8) | ip[i + 1]);
    while (sum >> 16) sum = (sum & 0xFFFF) + (sum >> 16);
    return (uint16_t)sum;
}

/* Minimal Ethernet+IPv4 frame with a correct checksum. */
static uint32_t build_pkt(uint8_t *p, uint8_t ttl, int vlan)
{
    uint8_t *ip;
    uint32_t off = vlan ? 18 : 14;
    uint16_t ck;

    memset(p, 0, 64 + (vlan ? 4 : 0));
    memset(p + 0, 0xAA, 6);              /* dst mac, to be rewritten */
    memset(p + 6, 0xBB, 6);              /* src mac, to be rewritten */
    if (vlan) {
        p[12] = 0x81; p[13] = 0x00;      /* 802.1Q */
        p[14] = 0x00; p[15] = 0x64;      /* vid 100 */
        p[16] = 0x08; p[17] = 0x00;      /* inner: IPv4 */
    } else {
        p[12] = 0x08; p[13] = 0x00;
    }

    ip = p + off;
    ip[0] = 0x45;                        /* v4, 5-word header */
    ip[2] = 0x00; ip[3] = 0x28;          /* total length 40 */
    ip[8] = ttl;
    ip[9] = 6;                           /* TCP */
    ip[12] = 192; ip[13] = 0; ip[14] = 2;  ip[15] = 10;   /* src */
    ip[16] = 10;  ip[17] = 1; ip[18] = 2;  ip[19] = 3;    /* dst */
    ip[10] = ip[11] = 0;
    ck = (uint16_t)~ip_csum(ip, 20);
    ip[10] = (uint8_t)(ck >> 8); ip[11] = (uint8_t)(ck & 0xFF);
    return off;
}

static void test_lpm(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;

    CHECK(dp_l3_init(&l3, 256, 256) == DP_L3_OK, "init");

    /* Deliberately inserted shortest-first, so a correct result cannot come
     * from insertion order -- only from the lookup actually being longest
     * prefix match. */
    CHECK(dp_l3_route_add(&l3, 0, 0, ip4(172,16,0,1), 1) == DP_L3_OK, "default");
    CHECK(dp_l3_route_add(&l3, ip4(10,0,0,0), 8, ip4(172,16,0,2), 2) == DP_L3_OK, "/8");
    CHECK(dp_l3_route_add(&l3, ip4(10,1,0,0), 16, 0, 3) == DP_L3_OK, "/16");
    CHECK(dp_l3_route_add(&l3, ip4(10,1,2,3), 32, ip4(172,16,0,4), 4) == DP_L3_OK, "/32");

    CHECK(dp_l3_lookup(&l3, ip4(10,1,2,3), &nh) == DP_L3_OK && nh.egress == 4,
          "exact /32 should win, got egress %u", nh.egress);
    CHECK(dp_l3_lookup(&l3, ip4(10,1,2,4), &nh) == DP_L3_OK && nh.egress == 3,
          "/16 should win over /8, got egress %u", nh.egress);
    CHECK(dp_l3_lookup(&l3, ip4(10,2,0,1), &nh) == DP_L3_OK && nh.egress == 2,
          "/8 should win over default, got egress %u", nh.egress);
    CHECK(dp_l3_lookup(&l3, ip4(8,8,8,8), &nh) == DP_L3_OK && nh.egress == 1,
          "default should catch, got egress %u", nh.egress);

    /* directly connected: nexthop 0 means the destination is its own next hop */
    CHECK(dp_l3_lookup(&l3, ip4(10,1,9,9), &nh) == DP_L3_OK &&
          nh.nexthop == ip4(10,1,9,9),
          "connected route should resolve nexthop to dst, got %08x", nh.nexthop);
    /* gateway route keeps the gateway */
    CHECK(dp_l3_lookup(&l3, ip4(10,2,0,1), &nh) == DP_L3_OK &&
          nh.nexthop == ip4(172,16,0,2), "gateway nexthop");

    /* removing the /32 must fall back to the /16, which also exercises the
     * cluster-reinsert path in the delete */
    CHECK(dp_l3_route_del(&l3, ip4(10,1,2,3), 32) == DP_L3_OK, "del /32");
    CHECK(dp_l3_lookup(&l3, ip4(10,1,2,3), &nh) == DP_L3_OK && nh.egress == 3,
          "after deleting /32 the /16 should win, got egress %u", nh.egress);

    /* with every route gone, lookup must fail rather than return stale state */
    dp_l3_flush(&l3);
    CHECK(dp_l3_lookup(&l3, ip4(10,1,2,3), &nh) == DP_L3_ERR_NOROUTE,
          "empty table must miss");

    dp_l3_fini(&l3);
}

static void test_neigh(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t mac[6] = { 0x02, 0x11, 0x22, 0x33, 0x44, 0x55 };

    CHECK(dp_l3_init(&l3, 64, 64) == DP_L3_OK, "init");
    CHECK(dp_l3_route_add(&l3, ip4(10,0,0,0), 8, ip4(10,0,0,1), 7) == DP_L3_OK, "route");

    /* A route with no neighbour is still a HIT -- the caller decides whether to
     * punt for ARP -- but it must not claim a MAC it does not have. */
    CHECK(dp_l3_lookup(&l3, ip4(10,5,5,5), &nh) == DP_L3_OK, "lookup");
    CHECK(nh.have_mac == 0, "must not invent a MAC before ARP resolves");

    CHECK(dp_l3_neigh_add(&l3, ip4(10,0,0,1), mac) == DP_L3_OK, "neigh add");
    CHECK(dp_l3_lookup(&l3, ip4(10,5,5,5), &nh) == DP_L3_OK, "lookup 2");
    CHECK(nh.have_mac == 1 && memcmp(nh.mac, mac, 6) == 0, "resolved MAC");

    CHECK(dp_l3_neigh_del(&l3, ip4(10,0,0,1)) == DP_L3_OK, "neigh del");
    CHECK(dp_l3_lookup(&l3, ip4(10,5,5,5), &nh) == DP_L3_OK && nh.have_mac == 0,
          "MAC must go away when the neighbour does");

    dp_l3_fini(&l3);
}

static void test_rewrite(int vlan)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t pkt[96];
    uint8_t nhmac[6] = { 0x02, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE };
    uint8_t ifmac[6] = { 0x02, 0x99, 0x88, 0x77, 0x66, 0x55 };
    uint32_t off;
    uint8_t *ip;

    CHECK(dp_l3_init(&l3, 64, 64) == DP_L3_OK, "init");
    CHECK(dp_l3_route_add(&l3, ip4(10,1,0,0), 16, 0, 5) == DP_L3_OK, "route");
    CHECK(dp_l3_neigh_add(&l3, ip4(10,1,2,3), nhmac) == DP_L3_OK, "neigh");
    CHECK(dp_l3_iface_set_mac(&l3, 5, ifmac) == DP_L3_OK, "iface mac");

    off = build_pkt(pkt, 64, vlan);
    ip = pkt + off;
    CHECK(ip_csum(ip, 20) == 0xFFFF, "test fixture checksum must start valid");

    CHECK(dp_l3_lookup(&l3, ip4(10,1,2,3), &nh) == DP_L3_OK && nh.have_mac,
          "lookup+neigh");
    CHECK(dp_l3_rewrite(&l3, pkt, (uint32_t)sizeof(pkt), &nh) == DP_L3_OK, "rewrite");

    CHECK(ip[8] == 63, "TTL should be decremented, got %u", ip[8]);
    /* The independent check: recomputing the whole header must still validate.
     * This is what catches a byte-order mistake in the incremental update. */
    CHECK(ip_csum(ip, 20) == 0xFFFF,
          "checksum invalid after rewrite (sum %04x)%s",
          ip_csum(ip, 20), vlan ? " [vlan]" : "");
    CHECK(memcmp(pkt, nhmac, 6) == 0, "dst MAC should be the next hop");
    CHECK(memcmp(pkt + 6, ifmac, 6) == 0, "src MAC should be the egress port");

    /* TTL 1 must be refused, not forwarded as 0 */
    build_pkt(pkt, 1, vlan);
    CHECK(dp_l3_rewrite(&l3, pkt, (uint32_t)sizeof(pkt), &nh) == DP_L3_ERR_TTL,
          "TTL 1 must be refused");

    dp_l3_fini(&l3);
}

/* Decrementing TTL repeatedly must keep the checksum valid every time -- one
 * decrement can be right by luck, 60 in a row cannot. */
static void test_checksum_sweep(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint8_t pkt[96];
    uint8_t mac[6] = { 0x02, 1, 2, 3, 4, 5 };
    uint32_t off;
    uint8_t *ip;
    int i;

    CHECK(dp_l3_init(&l3, 64, 64) == DP_L3_OK, "init");
    dp_l3_route_add(&l3, ip4(10,1,0,0), 16, 0, 1);
    dp_l3_neigh_add(&l3, ip4(10,1,2,3), mac);
    dp_l3_lookup(&l3, ip4(10,1,2,3), &nh);

    off = build_pkt(pkt, 64, 0);
    ip = pkt + off;
    for (i = 0; i < 60; i++) {
        CHECK(dp_l3_rewrite(&l3, pkt, (uint32_t)sizeof(pkt), &nh) == DP_L3_OK,
              "rewrite %d", i);
        if (ip_csum(ip, 20) != 0xFFFF) {
            CHECK(0, "checksum broke at hop %d (ttl %u)", i, ip[8]);
            break;
        }
    }
    CHECK(ip[8] == 4, "after 60 hops TTL should be 4, got %u", ip[8]);
    dp_l3_fini(&l3);
}

/* Config parsing. Malformed input matters more than the happy path here: a
 * typo in a relayed route must be REJECTED, never silently rounded into a
 * route that black-holes traffic. */
static void test_config(void)
{
    struct dp_l3 l3;
    struct dp_l3_config_stats st;
    struct dp_l3_nh nh;
    uint8_t mac[6] = { 0x02, 0xaa, 0xbb, 0xcc, 0xdd, 0x01 };

    CHECK(dp_l3_init(&l3, 256, 256) == DP_L3_OK, "init");
    memset(&st, 0, sizeof(st));

    CHECK(dp_l3_config_line(&l3, "dp.l3.route.1", "10.1.0.0/16 dev 3", &st)
          == DP_L3_CFG_OK, "connected route");
    CHECK(dp_l3_config_line(&l3, "dp.l3.route.2",
          "10.2.0.0/16 via 10.1.0.254 dev 3", &st) == DP_L3_CFG_OK, "gw route");
    CHECK(dp_l3_config_line(&l3, "dp.l3.route.3", "0.0.0.0/0 via 172.16.0.1 dev 1",
          &st) == DP_L3_CFG_OK, "default route");
    CHECK(dp_l3_config_line(&l3, "dp.l3.neigh.1",
          "10.1.0.254 lladdr 02:aa:bb:cc:dd:01", &st) == DP_L3_CFG_OK, "neigh");
    CHECK(dp_l3_config_line(&l3, "dp.l3.iface.3.mac", "02:39:f1:cb:45:00", &st)
          == DP_L3_CFG_OK, "iface mac");
    CHECK(st.routes == 3 && st.neigh == 1 && st.ifaces == 1 && st.errors == 0,
          "counts: r=%u n=%u i=%u e=%u", st.routes, st.neigh, st.ifaces, st.errors);

    /* keys belonging to other subsystems are skipped, not errors */
    CHECK(dp_l3_config_line(&l3, "dp.inspect.enable", "1", &st) == DP_L3_CFG_SKIP,
          "foreign key must skip");
    CHECK(dp_l3_config_line(&l3, "all.platform", "pa5220", &st) == DP_L3_CFG_SKIP,
          "all.* must skip");
    CHECK(st.errors == 0, "skips must not count as errors");

    /* the parsed table must actually route */
    CHECK(dp_l3_lookup(&l3, ip4(10,2,7,7), &nh) == DP_L3_OK &&
          nh.egress == 3 && nh.nexthop == ip4(10,1,0,254) &&
          nh.have_mac && memcmp(nh.mac, mac, 6) == 0,
          "gateway route resolves through its neighbour");
    CHECK(dp_l3_lookup(&l3, ip4(10,1,5,5), &nh) == DP_L3_OK &&
          nh.nexthop == ip4(10,1,5,5) && !nh.have_mac,
          "connected route is its own next hop and unresolved");

    /* --- malformed input must be refused --- */
    {
        uint32_t before = st.errors;
        const char *bad[] = {
            "10.1.0.0 dev 3",              /* no prefix length            */
            "10.1.0.0/33 dev 3",           /* prefix length out of range  */
            "10.1.0.256/16 dev 3",         /* octet out of range          */
            "10.1.0.0/16",                 /* no dev                      */
            "10.1.0.0/16 dev abc",         /* dev not a number            */
            "10.1.0.0/16 via  dev 3",      /* via with no address         */
            "10.1.0.0/16 wat 3 dev 1",     /* unknown keyword             */
        };
        unsigned k;
        for (k = 0; k < sizeof(bad) / sizeof(bad[0]); k++)
            CHECK(dp_l3_config_line(&l3, "dp.l3.route.9", bad[k], &st)
                  != DP_L3_CFG_OK, "must reject route: %s", bad[k]);
        CHECK(st.errors == before + (sizeof(bad) / sizeof(bad[0])),
              "every malformed route should be counted once");
    }
    CHECK(dp_l3_config_line(&l3, "dp.l3.neigh.9", "10.0.0.1 lladdr 02:aa:bb", &st)
          != DP_L3_CFG_OK, "short MAC must be rejected");
    CHECK(dp_l3_config_line(&l3, "dp.l3.neigh.9", "10.0.0.1 02:aa:bb:cc:dd:ee", &st)
          != DP_L3_CFG_OK, "missing lladdr keyword must be rejected");

    dp_l3_fini(&l3);
}

/*
 * ECMP.
 *
 * The properties that matter for a dataplane, in order: a flow must not be
 * split (that is reordering, which TCP reads as loss), every member must be
 * reachable, and a freed group must never forward anywhere.
 */
static void test_ecmp(void)
{
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    uint32_t nhs[3] = { 0x0a000101u, 0x0a000201u, 0x0a000301u };
    uint16_t egr[3] = { 11, 22, 33 };
    int g, i;

    CHECK(dp_l3_init(&l3, 64, 64) == DP_L3_OK, "init");

    g = dp_l3_ecmp_add(&l3, nhs, egr, 3);
    CHECK(g > 0, "group id must be positive (1-based), got %d", g);
    CHECK(dp_l3_route_add_ecmp(&l3, 0x0b000000u, 8, (uint16_t)g) == DP_L3_OK,
          "add 11.0.0.0/8 via the group");

    /* Determinism: the same hash must always choose the same member, or a
     * flow gets split across paths and reordered. */
    {
        struct dp_l3_nh a, b;
        CHECK(dp_l3_lookup_hash(&l3, 0x0b010203u, 0x12345678u, &a) == DP_L3_OK, "lookup a");
        for (i = 0; i < 32; i++) {
            CHECK(dp_l3_lookup_hash(&l3, 0x0b010203u, 0x12345678u, &b) == DP_L3_OK, "lookup b");
            CHECK(a.egress == b.egress && a.nexthop == b.nexthop,
                  "same hash must pick the same member every time");
        }
    }

    /* Every member must be reachable by some hash -- a selector that can only
     * ever return member 0 would pass a determinism test and still be broken. */
    {
        int seen[3] = { 0, 0, 0 }, distinct = 0;
        uint32_t h;
        for (h = 0; h < 4096; h++) {
            CHECK(dp_l3_lookup_hash(&l3, 0x0b010203u, h, &nh) == DP_L3_OK, "sweep");
            for (i = 0; i < 3; i++)
                if (nh.egress == egr[i]) seen[i] = 1;
        }
        for (i = 0; i < 3; i++) distinct += seen[i];
        CHECK(distinct == 3, "all 3 members must be selectable, saw %d", distinct);
    }

    /* The plain wrapper must keep working and stay per-destination. */
    CHECK(dp_l3_lookup(&l3, 0x0b010203u, &nh) == DP_L3_OK, "plain lookup still routes");
    {
        struct dp_l3_nh again;
        dp_l3_lookup(&l3, 0x0b010203u, &again);
        CHECK(again.egress == nh.egress, "plain lookup is stable per destination");
    }

    /* A single-next-hop route must be completely unaffected by ECMP existing. */
    CHECK(dp_l3_route_add(&l3, 0x0c000000u, 8, 0x0a00ff01u, 77) == DP_L3_OK, "plain route");
    CHECK(dp_l3_lookup_hash(&l3, 0x0c010203u, 0x9999u, &nh) == DP_L3_OK, "plain route lookup");
    CHECK(nh.egress == 77 && nh.nexthop == 0x0a00ff01u,
          "non-ECMP route must ignore the flow hash entirely");

    /* Overwriting an ECMP route with a plain one must drop the group. */
    CHECK(dp_l3_route_add(&l3, 0x0b000000u, 8, 0x0a00ee01u, 88) == DP_L3_OK, "overwrite");
    CHECK(dp_l3_lookup_hash(&l3, 0x0b010203u, 0x1u, &nh) == DP_L3_OK, "after overwrite");
    CHECK(nh.egress == 88, "overwritten route must use the new single next hop, got %u",
          nh.egress);

    /* Freeing a group must not leave routes forwarding to egress 0. */
    CHECK(dp_l3_route_add_ecmp(&l3, 0x0d000000u, 8, (uint16_t)g) == DP_L3_OK, "re-point");
    CHECK(dp_l3_ecmp_del(&l3, (uint16_t)g) == DP_L3_OK, "delete the group");
    CHECK(dp_l3_lookup_hash(&l3, 0x0d010203u, 0x1u, &nh) == DP_L3_ERR_NOROUTE,
          "a route whose group was freed must be NOROUTE, not egress 0");

    /* Bounds. */
    CHECK(dp_l3_ecmp_add(&l3, nhs, egr, 0) == DP_L3_ERR_RANGE, "n=0 rejected");
    CHECK(dp_l3_ecmp_add(&l3, nhs, egr, DP_L3_ECMP_MAX + 1) == DP_L3_ERR_RANGE,
          "n > max rejected");
    CHECK(dp_l3_route_add_ecmp(&l3, 0x0e000000u, 8, 0) == DP_L3_ERR_RANGE,
          "group 0 is not a group");
    CHECK(dp_l3_route_add_ecmp(&l3, 0x0e000000u, 8, 999) == DP_L3_ERR_RANGE,
          "unknown group rejected");
    CHECK(dp_l3_ecmp_del(&l3, 0) == DP_L3_ERR_RANGE, "deleting group 0 rejected");

    /* A directly-connected member (nexthop 0) must resolve to the packet's
     * destination, exactly as a single-next-hop route does. */
    {
        uint32_t z[1] = { 0 };
        uint16_t e[1] = { 5 };
        int g2 = dp_l3_ecmp_add(&l3, z, e, 1);
        CHECK(g2 > 0, "single-member group");
        CHECK(dp_l3_route_add_ecmp(&l3, 0x0f000000u, 8, (uint16_t)g2) == DP_L3_OK, "route");
        CHECK(dp_l3_lookup_hash(&l3, 0x0f010203u, 0x7u, &nh) == DP_L3_OK, "lookup");
        CHECK(nh.nexthop == 0x0f010203u,
              "nexthop 0 in a group means directly connected");
    }

    dp_l3_fini(&l3);
}

/* Multipath from the config, which is how an operator reaches ECMP at all.
 *
 * dp_l3_ecmp_add() had existed since the ECMP work with no caller outside
 * these tests, so the FIB could do ECMP and no configuration could ask for it.
 */
static void test_config_multipath(void)
{
    struct dp_l3_config_stats st;
    struct dp_l3 l3;
    struct dp_l3_nh nh;
    int seen[2], h, distinct;

    memset(&st, 0, sizeof(st));
    CHECK(dp_l3_init(&l3, 64, 64) == DP_L3_OK, "init");

    CHECK(dp_l3_config_line(&l3, "dp.l3.route.1",
              "10.0.0.0/8 nexthop via 172.16.0.1 dev 11"
              " nexthop via 172.16.0.2 dev 22", &st)
          == DP_L3_CFG_OK, "two-leg multipath");
    CHECK(st.routes == 1 && st.errors == 0,
          "routes=%u errors=%u", st.routes, st.errors);

    seen[0] = seen[1] = 0;
    for (h = 0; h < 512; h++) {
        CHECK(dp_l3_lookup_hash(&l3, 0x0A010203u, (uint32_t)h, &nh) == DP_L3_OK,
              "sweep");
        if (nh.egress == 11) seen[0] = 1;
        else if (nh.egress == 22) seen[1] = 1;
        else CHECK(0, "unexpected egress %u", nh.egress);
    }
    distinct = seen[0] + seen[1];
    CHECK(distinct == 2, "both legs must be reachable, saw %d", distinct);

    /* A rejected multipath installs NOTHING. A half-built group holding the
     * legs that happened to parse first is worse than a rejected line,
     * because it forwards. */
    {
        uint32_t before = st.routes;
        CHECK(dp_l3_config_line(&l3, "dp.l3.route.2",
                  "10.2.0.0/16 nexthop via 172.16.0.1 dev 11"
                  " nexthop via 172.16.0.2", &st) < 0, "leg without dev");
        CHECK(dp_l3_config_line(&l3, "dp.l3.route.3",
                  "10.3.0.0/16 nexthop via 999.1.1.1 dev 11"
                  " nexthop via 172.16.0.2 dev 22", &st) < 0, "bad gateway");
        CHECK(dp_l3_config_line(&l3, "dp.l3.route.4",
                  "10.4.0.0/16 nexthop wat 1 dev 11", &st) < 0, "unknown keyword");
        CHECK(st.routes == before, "nothing installed from a rejected multipath");
    }

    /* A leg with no "via" is directly connected -- the same meaning a via-less
     * single path has. */
    CHECK(dp_l3_config_line(&l3, "dp.l3.route.5",
              "10.5.0.0/16 nexthop dev 7 nexthop via 172.16.0.9 dev 8", &st)
          == DP_L3_CFG_OK, "a connected leg is legal");

    /* Exactly DP_L3_ECMP_MAX legs is the largest LEGAL multipath and must be
     * accepted -- the case that catches a token budget sized to the maximum
     * rather than past it. */
    {
        char ok[DP_L3_CFG_MAX_VALUE];
        int i, off = 0;
        off += sprintf(ok + off, "10.8.0.0/16");
        for (i = 0; i < (int)DP_L3_ECMP_MAX; i++)
            off += sprintf(ok + off, " nexthop via 172.16.0.%d dev %d",
                           i + 1, i + 1);
        CHECK(dp_l3_config_line(&l3, "dp.l3.route.8", ok, &st)
              == DP_L3_CFG_OK, "DP_L3_ECMP_MAX legs must be ACCEPTED");
    }

    /* More legs than a group can hold is a rejection, not a truncation. */
    {
        char big[DP_L3_CFG_MAX_VALUE];
        int i, off = 0;
        off += sprintf(big + off, "10.9.0.0/16");
        for (i = 0; i < (int)DP_L3_ECMP_MAX + 1; i++)
            off += sprintf(big + off, " nexthop via 172.16.0.%d dev %d",
                           i + 1, i + 1);
        CHECK(dp_l3_config_line(&l3, "dp.l3.route.6", big, &st) < 0,
              "more than DP_L3_ECMP_MAX legs must be rejected");
    }

    /* The single-path form must NOT become a one-member group: that is a
     * different object in the FIB and would change what an existing config
     * resolves to. */
    CHECK(dp_l3_config_line(&l3, "dp.l3.route.7", "10.7.0.0/16 via 172.16.0.1 dev 3",
                            &st) == DP_L3_CFG_OK, "plain single path still works");
    CHECK(dp_l3_lookup(&l3, 0x0A070001u, &nh) == DP_L3_OK, "single-path lookup");
    CHECK(nh.egress == 3, "egress 3, got %u", nh.egress);

    dp_l3_fini(&l3);
}

int main(void)
{
    printf("ffn_dp_l3_test (%s-endian)\n",
           (*(const uint16_t *)"\x01\x02" == 0x0102) ? "big" : "little");

    test_lpm();
    test_neigh();
    test_rewrite(0);
    test_rewrite(1);          /* same again with a VLAN tag in the way */
    test_checksum_sweep();
    test_config();
    test_config_multipath();
    test_ecmp();

    if (fails == 0) printf("PASS: all L3 tests\n");
    else            printf("FAIL: %d check(s)\n", fails);
    return fails ? 1 : 0;
}
