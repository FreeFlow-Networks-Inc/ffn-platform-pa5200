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

    if (fails == 0) printf("PASS: all L3 tests\n");
    else            printf("FAIL: %d check(s)\n", fails);
    return fails ? 1 : 0;
}
