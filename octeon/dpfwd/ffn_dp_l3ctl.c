/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3ctl -- load the relayed config into a FIB and answer lookups.
 *
 * This is the proof that the whole southbound chain actually lands somewhere:
 *
 *   MP /etc/ffn/config.env -> ffn_cfgd -> ffn_cfgagent (CP)
 *     -> PCIe mailbox -> DP /etc/ffn/dp.env -> dp_l3_config_apply() -> FIB
 *
 * Run it on the DP against the file the relay delivered, and it reports what
 * was applied and resolves destinations against the resulting table. It is a
 * diagnostic, not the forwarder: the forwarder calls the same
 * dp_l3_config_apply() and then routes packets with the same dp_l3_lookup().
 *
 *   ffn_dp_l3ctl <config-file> [dst-ip ...]
 *
 * Static-linked for the DP and CP, which are mips64 big-endian on glibc 2.16
 * while the cross toolchain is far newer.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ffn_dp_l3_config.h"

static void print_ip(uint32_t ip)
{
    printf("%u.%u.%u.%u", (ip >> 24) & 0xFF, (ip >> 16) & 0xFF,
           (ip >> 8) & 0xFF, ip & 0xFF);
}

static int parse_ip(const char *s, uint32_t *out)
{
    unsigned a, b, c, d;
    char extra;
    if (sscanf(s, "%u.%u.%u.%u%c", &a, &b, &c, &d, &extra) != 4) return -1;
    if (a > 255 || b > 255 || c > 255 || d > 255) return -1;
    *out = (a << 24) | (b << 16) | (c << 8) | d;
    return 0;
}

int main(int argc, char **argv)
{
    struct dp_l3 l3;
    struct dp_l3_config_stats st;
    int i, rc;

    if (argc < 2) {
        fprintf(stderr, "usage: %s <config-file> [dst-ip ...]\n", argv[0]);
        return 2;
    }

    if (dp_l3_init(&l3, 1024, 1024) != DP_L3_OK) {
        fprintf(stderr, "l3 init failed\n");
        return 1;
    }

    rc = dp_l3_config_apply(&l3, argv[1], &st);
    if (rc == DP_L3_CFG_ERR_OPEN) {
        fprintf(stderr, "cannot open %s\n", argv[1]);
        return 1;
    }

    printf("applied %s: %u route(s), %u neighbour(s), %u iface mac(s), "
           "%u ignored, %u error(s)\n",
           argv[1], st.routes, st.neigh, st.ifaces, st.ignored, st.errors);

    for (i = 2; i < argc; i++) {
        struct dp_l3_nh nh;
        uint32_t dst;

        if (parse_ip(argv[i], &dst) != 0) {
            printf("  %-15s BAD ADDRESS\n", argv[i]);
            continue;
        }
        printf("  %-15s -> ", argv[i]);
        if (dp_l3_lookup(&l3, dst, &nh) != DP_L3_OK) {
            printf("no route\n");
            continue;
        }
        printf("dev %u via ", nh.egress);
        print_ip(nh.nexthop);
        if (nh.have_mac)
            printf(" lladdr %02x:%02x:%02x:%02x:%02x:%02x\n",
                   nh.mac[0], nh.mac[1], nh.mac[2],
                   nh.mac[3], nh.mac[4], nh.mac[5]);
        else
            printf(" (unresolved -- would punt for ARP)\n");
    }

    dp_l3_fini(&l3);
    return st.errors ? 1 : 0;
}
