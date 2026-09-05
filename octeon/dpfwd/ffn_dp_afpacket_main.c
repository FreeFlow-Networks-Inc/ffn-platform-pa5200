/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_afpacket_main.c -- runnable FFN dataplane over AF_PACKET.
 *
 * Runs the OCTEON-II forwarder (ffn_dp_oct.c) against real Linux interfaces, so
 * the whole path -- policy tables, classify, flow cache, verdicts, forwarding --
 * is exercisable end-to-end without Octeon hardware.
 *
 *   ffn_dp_afpacket -i vethA0 -i vethB0 -p policy.bin [-d drop|forward] [-P] [-s 2]
 *
 *   -i IFACE     add a port (repeatable; port index = order, matches the policy
 *                `egress` field). Two ports with no egress = bump-in-the-wire.
 *   -p FILE      policy.bin from ffn_fastpath_compile.py (type 0x40 "FPPO")
 *   -d DECISION  default when no rule matches: drop (default) | forward | local
 *   -v N         vsys tag applied to frames (default 1)
 *   -P           promiscuous mode on each port
 *   -s SEC       print stats every SEC seconds (0 = only at exit)
 *   -c N         stop after N packets (0 = run until signalled) -- for tests
 */
#define _GNU_SOURCE
#include "ffn_dp_abi.h"
#include "ffn_dp_oct.h"
#include "ffn_dp_io_afpacket.h"
#include "ffn_dp_engine.h"
#include "ffn_dp_dlp.h"
#include "ffn_dp_vsys.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t g_stop;
static void on_sig(int s) { (void)s; g_stop = 1; }

static void *slurp(const char *path, size_t *len_out)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long n = ftell(f);
    if (n <= 0) { fclose(f); return NULL; }
    rewind(f);
    void *buf = malloc((size_t)n);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)n, f);
    fclose(f);
    if (got != (size_t)n) { free(buf); return NULL; }
    *len_out = got;
    return buf;
}

static void usage(const char *a0)
{
    fprintf(stderr,
        "usage: %s -i IFACE [-i IFACE ...] [-p policy.bin] [-d drop|forward|local]\n"
        "          [-v vsys] [-P] [-s sec] [-c count] [-e dp.env]\n"
        "  -e   inline analysis engines: dp.engine.*/dp.dlp.* from a key=value\n"
        "       file, the same one the control plane delivers. Without it the\n"
        "       engines are not attached and nothing is scanned.\n", a0);
}

/*
 * Build the inline analysis engine set from a config file, and attach it.
 *
 * Without this the engines are inert in production: dp_process() will run them
 * when a set is attached, and nothing outside the unit test ever attached one.
 * The file is the same key=value dp.env the control plane already delivers, so
 * the rules an operator enters in the WebUI reach the forwarder through one
 * path rather than two.
 *
 *     dp.engine.<name>.enable = 0|1
 *     dp.dlp.rule.<id>        = <type>:<action>:<direction>:<pattern>
 *
 * Storage is static and file-scope on purpose: the engine set and the DLP rule
 * table must outlive this function and must not be allocated, because the
 * forwarder dereferences them on the packet path.
 */
static struct dp_engine_set g_engines;
static struct dp_dlp        g_dlp;

static int load_engines(struct dp_ctx *dp, const char *path)
{
    FILE *f = fopen(path, "r");
    char line[512];
    int rules = 0, bad = 0;

    if (!f) {
        fprintf(stderr, "engines: cannot read %s: %s\n", path, strerror(errno));
        return -1;
    }

    memset(&g_engines, 0, sizeof(g_engines));
    memset(&g_dlp, 0, sizeof(g_dlp));
    if (dp_engine_register(&g_engines, "dlp", dp_dlp_scan, &g_dlp) < 0) {
        fprintf(stderr, "engines: cannot register dlp\n");
        fclose(f);
        return -1;
    }

    while (fgets(line, sizeof(line), f)) {
        char *eq, *key, *val, *nl;
        if (line[0] == '#' || line[0] == '\n')
            continue;
        nl = strchr(line, '\n');
        if (nl)
            *nl = '\0';
        eq = strchr(line, '=');
        if (!eq)
            continue;
        *eq = '\0';
        key = line;
        val = eq + 1;

        /* A malformed rule is REPORTED, never skipped in silence. A DLP rule
         * that quietly failed to load leaves an operator looking at a policy
         * they believe is enforced while nothing on the wire enforces it. */
        int rc = dp_dlp_config_line(&g_dlp, key, val);
        if (rc == 1) {
            rules++;
            continue;
        }
        if (rc < 0) {
            fprintf(stderr, "engines: bad rule %s=%s\n", key, val);
            bad++;
            continue;
        }
        if (dp_engine_config_line(&g_engines, key, val))
            continue;
    }
    fclose(f);

    if (bad) {
        /* Fail closed on a malformed policy rather than run a partial one:
         * "some of your DLP rules are loaded" is not a state an operator can
         * reason about. */
        fprintf(stderr, "engines: %d malformed rule(s) in %s; not attaching\n",
                bad, path);
        return -1;
    }

    dp_engine_attach(dp, &g_engines);
    printf("engines: %u registered, %d dlp rule(s) from %s\n",
           g_engines.count, rules, path);
    for (uint32_t i = 0; i < g_engines.count; i++)
        printf("  %-12s %s\n", g_engines.e[i].name,
               g_engines.e[i].enabled ? "enabled" : "disabled");
    return 0;
}

/*
 * Virtual systems, read from the same dp.env the control plane delivers.
 *
 *     dp.vsys.tenant.<id> = <name>     which tenants exist
 *     dp.vsys.port.<idx>  = <id>       which port belongs to which tenant
 *
 * This is the link that made vsys real rather than decorative. The forwarder
 * has always carried a per-packet vsys byte, but it came from a single -v
 * applied to every port, so a second tenant could be created, given a port, and
 * committed, and its packets still arrived tagged as the first.
 *
 * Two things happen with what is parsed here. The per-port assignment is used
 * when the ports are added, which is what fixes the -v problem on the path that
 * runs today. The tenant list becomes a dp_vsys_plan -- one SSO group, one QPG
 * entry and one PKI style each -- which is what the OCTEON backend applies to
 * the chip so the tenant is decided by PKI at wire speed instead of by
 * software. On AF_PACKET there is no PKI, so the plan is built, reported and
 * held: it is what a later oct backend attaches, and building it here means the
 * two paths cannot disagree about which tenant owns which group.
 */
static struct dp_vsys_plan g_vsys_plan;
static uint8_t g_port_vsys[AFP_MAX_PORTS];

struct vsys_cfg {
    uint8_t  ids[DP_VSYS_MAX];
    uint32_t n;
    int      ports_seen;
};

static int vsys_add_tenant(struct vsys_cfg *v, long id)
{
    uint32_t i;

    if (id < 1 || id > (long)DP_VSYS_MAX)
        return -1;
    for (i = 0; i < v->n; i++)
        if (v->ids[i] == (uint8_t)id)
            return 0;               /* already known; not an error */
    if (v->n >= DP_VSYS_MAX)
        return -1;
    v->ids[v->n++] = (uint8_t)id;
    return 0;
}

/* Parse the vsys keys out of a dp.env. Returns 0, or -1 if the file names
 * something it cannot express -- which is refused rather than partly applied,
 * because a tenant boundary that is half configured is not a boundary. */
static int load_vsys(const char *path, struct vsys_cfg *v)
{
    FILE *f = fopen(path, "r");
    char line[512];
    int bad = 0;

    memset(v, 0, sizeof(*v));
    memset(g_port_vsys, 0, sizeof(g_port_vsys));
    if (!f) {
        fprintf(stderr, "vsys: cannot read %s: %s\n", path, strerror(errno));
        return -1;
    }

    while (fgets(line, sizeof(line), f)) {
        char *eq, *nl, *key, *val;
        long a, b;

        if (line[0] == '#' || line[0] == '\n')
            continue;
        nl = strchr(line, '\n');
        if (nl)
            *nl = '\0';
        eq = strchr(line, '=');
        if (!eq)
            continue;
        *eq = '\0';
        key = line;
        val = eq + 1;

        if (strncmp(key, "dp.vsys.tenant.", 15) == 0) {
            a = strtol(key + 15, NULL, 10);
            if (vsys_add_tenant(v, a) != 0) {
                fprintf(stderr, "vsys: tenant id %ld is outside 1..%u\n",
                        a, DP_VSYS_MAX);
                bad++;
            }
        } else if (strncmp(key, "dp.vsys.port.", 13) == 0) {
            a = strtol(key + 13, NULL, 10);          /* DP port index */
            b = strtol(val, NULL, 10);               /* tenant id     */
            if (a < 0 || a >= AFP_MAX_PORTS) {
                fprintf(stderr, "vsys: port index %ld out of range\n", a);
                bad++;
                continue;
            }
            if (b < 1 || b > (long)DP_VSYS_MAX) {
                fprintf(stderr, "vsys: port %ld assigned tenant %ld, "
                                "outside 1..%u\n", a, b, DP_VSYS_MAX);
                bad++;
                continue;
            }
            /* A port assigned to a tenant the file never declares would be
             * tagged with an id that has no hardware resources behind it. Take
             * it as a declaration too, so the two keys cannot disagree. */
            if (vsys_add_tenant(v, b) != 0) {
                bad++;
                continue;
            }
            g_port_vsys[a] = (uint8_t)b;
            v->ports_seen++;
        }
    }
    fclose(f);

    if (bad) {
        fprintf(stderr, "vsys: %d bad key(s) in %s; not applying any of it\n",
                bad, path);
        return -1;
    }
    return 0;
}

/* Build the hardware plan and report it. Bases start at 8/16/4 rather than 0
 * because group 0, QPG 0 and style 0 belong to the SDK's own default path, and
 * a plan starting there would quietly take them over. */
static int plan_vsys(const struct vsys_cfg *v)
{
    uint32_t i;
    int rc;

    rc = dp_vsys_plan_build(&g_vsys_plan, v->ids, v->n,
                            &DP_VSYS_LIMITS_CN78XX, 8, 16, 4);
    if (rc != DP_OK) {
        fprintf(stderr, "vsys: cannot plan %u tenant(s): %s\n",
                v->n, dp_strerror(rc));
        return -1;
    }
    if (v->n == 0) {
        printf("vsys: none configured -- every packet is the wildcard, which is "
               "single-vsys behaviour\n");
        return 0;
    }
    printf("vsys: %u tenant(s), %d port assignment(s)\n", v->n, v->ports_seen);
    for (i = 0; i < g_vsys_plan.count; i++) {
        const struct dp_vsys_res *r = &g_vsys_plan.res[i];
        printf("  vsys %-3u sso_group=%-4u qpg=%-4u style=%u\n",
               r->vsys, r->sso_group, r->qpg_offset, r->style);
    }
    return 0;
}


int main(int argc, char **argv)
{
    const char *ifaces[AFP_MAX_PORTS];
    int nif = 0;
    const char *polpath = NULL;
    const char *engpath = NULL;
    int default_dec = FP_DROP_W;
    int promisc = 0, stats_sec = 0;
    unsigned long stop_after = 0;
    uint8_t vsys = 1;

    int opt;
    while ((opt = getopt(argc, argv, "i:p:d:v:Ps:c:e:h")) != -1) {
        switch (opt) {
        case 'i':
            if (nif >= AFP_MAX_PORTS) { fprintf(stderr, "too many ports\n"); return 2; }
            ifaces[nif++] = optarg;
            break;
        case 'p': polpath = optarg; break;
        case 'e': engpath = optarg; break;
        case 'd':
            if (!strcmp(optarg, "drop"))         default_dec = FP_DROP_W;
            else if (!strcmp(optarg, "forward")) default_dec = FP_FORWARD_W;
            else if (!strcmp(optarg, "local"))   default_dec = FP_LOCAL_W;
            else { fprintf(stderr, "bad -d %s\n", optarg); return 2; }
            break;
        case 'v': vsys = (uint8_t)atoi(optarg); break;
        case 'P': promisc = 1; break;
        case 's': stats_sec = atoi(optarg); break;
        case 'c': stop_after = strtoul(optarg, NULL, 10); break;
        default: usage(argv[0]); return 2;
        }
    }
    if (nif == 0) { usage(argv[0]); return 2; }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    /* Tenants BEFORE the ports, because a port tenant is decided as it is
     * added. Parsed from the same dp.env the engines come from: one file the
     * control plane delivers, not two that can disagree. */
    struct vsys_cfg vcfg;
    memset(&vcfg, 0, sizeof(vcfg));
    if (engpath) {
        if (load_vsys(engpath, &vcfg) != 0)
            return 1;
        if (plan_vsys(&vcfg) != 0)
            return 1;
    }

    struct afp_ctx io;
    afp_ctx_init(&io, promisc, 100);
    for (int i = 0; i < nif; i++) {
        /* The config assigns tenants by DP PORT INDEX, and ports are added in
         * the order given on the command line, so index i is this port. The
         * global -v stays the fallback for a port the config does not mention
         * -- which is every port on a box with no tenants, and is how this
         * keeps behaving exactly as it did before vsys existed. */
        uint8_t pv = g_port_vsys[i] ? g_port_vsys[i] : vsys;
        int rc = afp_add_port(&io, ifaces[i], pv);
        if (rc < 0) {
            fprintf(stderr, "port %s: %s\n", ifaces[i], strerror(-rc));
            return 1;
        }
        if (rc != i) {
            /* The tenant map is indexed by the position a port was added at.
             * If the backend ever numbered them differently the assignment
             * would land on the wrong port silently, so check rather than
             * assume. */
            fprintf(stderr, "port %s: got index %d, expected %d -- refusing a"
                            " tenant map that may be misaligned\n",
                    ifaces[i], rc, i);
            return 1;
        }
        printf("port %d = %s", rc, ifaces[i]);
        if (g_port_vsys[i])
            printf("  vsys %u", g_port_vsys[i]);
        printf("\n");
    }

    struct dp_ctx dp;
    int rc = dp_init(&dp, &AFP_IO, &io, 0);
    if (rc != DP_OK) {
        fprintf(stderr, "dp_init: %s\n", dp_strerror(rc));
        return 1;
    }
    dp.default_decision = default_dec;

    /* A shared region gives us the same handshake + bank/ring machinery the
     * Octeon uses; here it is ordinary memory instead of a PCIe BAR. */
    size_t rsz = FFN_DP_OFF_BANK0 + (4u << 20);
    void *region = calloc(1, rsz);
    if (!region) { fprintf(stderr, "region alloc failed\n"); return 1; }
    rc = dp_region_attach(&dp, region, rsz, 1);
    if (rc != DP_OK) {
        fprintf(stderr, "region attach: %s\n", dp_strerror(rc));
        return 1;
    }

    /* Engines before the first packet: attaching mid-run would mean the flows
     * already in the cache were classified without analysis and would keep
     * their unanalysed verdict. */
    if (engpath && load_engines(&dp, engpath) != 0)
        return 1;
    if (!engpath)
        printf("engines: none attached (-e to load dp.engine.*/dp.dlp.*)\n");

    if (polpath) {
        size_t plen = 0;
        void *pol = slurp(polpath, &plen);
        if (!pol) { fprintf(stderr, "cannot read %s\n", polpath); return 1; }
        if (plen > FFN_DP_BANK_SIZE) { fprintf(stderr, "policy too large\n"); return 1; }
        memcpy((uint8_t *)region + FFN_DP_OFF_BANK0, pol, plen);
        free(pol);
        rc = dp_activate_bank(&dp, 0);
        if (rc != DP_OK) {
            fprintf(stderr, "policy load: %s\n", dp_strerror(rc));
            return 1;
        }
        printf("policy: %u rule(s) from %s\n", dp.tables.policy_n, polpath);
    } else {
        printf("policy: none loaded -- every packet takes the default (%s)\n",
               default_dec == FP_DROP_W ? "drop" :
               default_dec == FP_FORWARD_W ? "forward" : "local");
    }
    printf("default decision: %s\nrunning (ctrl-c to stop)\n",
           default_dec == FP_DROP_W ? "drop" :
           default_dec == FP_FORWARD_W ? "forward" : "local");
    fflush(stdout);

    dp_set_state(&dp, DP_STATE_READY);
    time_t last = time(NULL);
    while (!g_stop) {
        dp_poll_once(&dp);
        if (stop_after && dp.stat_rx >= stop_after)
            break;
        if (stats_sec) {
            time_t now = time(NULL);
            if (now - last >= stats_sec) {
                last = now;
                printf("rx=%llu fwd=%llu insp=%llu drop=%llu local=%llu "
                       "cache_hit=%llu classify=%llu parse_err=%llu\n",
                       (unsigned long long)dp.stat_rx,
                       (unsigned long long)dp.stat_forward,
                       (unsigned long long)dp.stat_inspect,
                       (unsigned long long)dp.stat_drop,
                       (unsigned long long)dp.stat_local,
                       (unsigned long long)dp.stat_cache_hit,
                       (unsigned long long)dp.stat_classify,
                       (unsigned long long)dp.stat_parse_err);
                fflush(stdout);
            }
        }
    }

    printf("\n--- final ---\n");
    printf("dp: rx=%llu tx=%llu tx_fail=%llu fwd=%llu insp=%llu drop=%llu "
           "punt=%llu local=%llu cache_hit=%llu classify=%llu parse_err=%llu "
           "flow_full=%llu flows=%u\n",
           (unsigned long long)dp.stat_rx, (unsigned long long)dp.stat_tx,
           (unsigned long long)dp.stat_tx_fail,
           (unsigned long long)dp.stat_forward, (unsigned long long)dp.stat_inspect,
           (unsigned long long)dp.stat_drop, (unsigned long long)dp.stat_punt,
           (unsigned long long)dp.stat_local, (unsigned long long)dp.stat_cache_hit,
           (unsigned long long)dp.stat_classify,
           (unsigned long long)dp.stat_parse_err,
           (unsigned long long)dp.stat_flow_full, dp.flows.count);
    afp_dump_stats(&io, stdout);

    dp_fini(&dp);
    free(region);
    return 0;
}
