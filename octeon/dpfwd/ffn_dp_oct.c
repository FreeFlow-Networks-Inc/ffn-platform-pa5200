/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_oct.c -- FFN dataplane forwarder for OCTEON-II (PA-5220 "Gryphon").
 *
 * Consumes the SAME fastpath tables ffn_fastpath_compile.py already produces for
 * the x86 DPDK fastpath (policy.bin, type 0x40 "FPPO", 32-byte rows) -- one
 * policy compiler, two dataplanes. Table rows are little-endian on the wire and
 * are converted to native structs ONCE at load time, so the per-packet path
 * never byte-swaps even though OCTEON-II is big-endian.
 *
 * Deliberately free of chip-specific includes: packet I/O sits behind
 * struct dp_io_ops, so the same object builds
 *   * natively (x86-64) against the "sim" backend for the test harness, and
 *   * for mips64-linux (OCTEON-II) against a PKI/PKO or AF_PACKET backend.
 * That keeps the forwarding logic testable without the hardware present.
 *
 * Pipeline (mirrors ffn_fastpath_fwd.c):
 *   rx -> parse L2/L3/L4 -> normalized flow key (+vsys)
 *      -> flow-cache lookup   hit : apply cached verdict
 *                            miss : classify() vs policy rows, cache result
 *      -> FP_FORWARD  : tx
 *         FP_INSPECT  : tx + mark for inspection (payload scan is a later stage)
 *         FP_DROP     : drop (+RST for FP_V_RESET)
 *         FP_PUNT_FPGA: hand to the FE100/FPGA path (backend-specific)
 *         FP_LOCAL    : hand to the local stack
 */
#include "ffn_dp_abi.h"
#include "ffn_dp_oct.h"
#include "ffn_dp_arp.h"
#include "ffn_dp_engine.h"
#include "ffn_dp_bgx.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* ---- on-wire fastpath table layout (from ffn_fastpath.h / the compiler) ---- */
#define FP_HDR_SIZE_W     36u
#define FP_POLICY_SIZE_W  32u
#define FP_T_POLICY_W     0x40u
#define FP_MAGIC_POLICY_W "FPPO"

/* ------------------------------------------------------------------ */
/* table loading: little-endian wire rows -> native rows              */
/* ------------------------------------------------------------------ */
int dp_tables_load(struct dp_tables *t, const void *blob, size_t len)
{
    memset(t, 0, sizeof(*t));
    if (!blob || len < FP_HDR_SIZE_W)
        return DP_ERR_SHORT;

    const uint8_t *p = (const uint8_t *)blob;
    if (memcmp(p, FP_MAGIC_POLICY_W, 4) != 0)
        return DP_ERR_MAGIC;

    uint16_t ver   = ld_le16(p + 4);
    uint16_t type  = ld_le16(p + 6);
    uint32_t count = ld_le32(p + 8);
    uint32_t recsz = ld_le32(p + 24);

    if (type != FP_T_POLICY_W)
        return DP_ERR_TYPE;
    if (recsz != FP_POLICY_SIZE_W)
        return DP_ERR_RECSZ;
    if ((size_t)count * FP_POLICY_SIZE_W + FP_HDR_SIZE_W > len)
        return DP_ERR_SHORT;
    if (count > DP_MAX_POLICY)
        return DP_ERR_TOOMANY;

    t->version = ver;
    t->policy_n = count;
    const uint8_t *row = p + FP_HDR_SIZE_W;
    for (uint32_t i = 0; i < count; i++, row += FP_POLICY_SIZE_W) {
        struct dp_policy_row *r = &t->policy[i];
        /* IPv4 fields are stored as NETWORK-ORDER BYTES -> read big-endian to
         * get the host value. (Using ld_le32 + a host ntohl here works on x86
         * but returns byte-swapped addresses on big-endian OCTEON-II; the
         * cross-endian test catches exactly that.) */
        r->src_ip   = ld_be32(row + 0);
        r->src_mask = ld_be32(row + 4);
        r->dst_ip   = ld_be32(row + 8);
        r->dst_mask = ld_be32(row + 12);
        r->sport_lo = ld_le16(row + 16);
        r->sport_hi = ld_le16(row + 18);
        r->dport_lo = ld_le16(row + 20);
        r->dport_hi = ld_le16(row + 22);
        r->proto    = row[24];
        r->vsys     = row[25];
        r->action   = row[26];
        r->flags    = row[27];
        r->egress   = ld_le16(row + 28);
        r->rule_id  = ld_le16(row + 30);
    }
    return DP_OK;
}

/* ------------------------------------------------------------------ */
/* classify: first matching row wins (mirrors the x86 fastpath)        */
/* ------------------------------------------------------------------ */
int dp_classify(const struct dp_tables *t, const struct dp_tuple *k,
                uint16_t *rule_id, uint16_t *egress)
{
    for (uint32_t i = 0; i < t->policy_n; i++) {
        const struct dp_policy_row *r = &t->policy[i];

        if (r->vsys && r->vsys != k->vsys)          /* 0 = any vsys      */
            continue;
        if (r->proto && r->proto != k->proto)       /* 0 = any proto     */
            continue;
        if (r->src_mask && ((k->src_ip & r->src_mask) != (r->src_ip & r->src_mask)))
            continue;
        if (r->dst_mask && ((k->dst_ip & r->dst_mask) != (r->dst_ip & r->dst_mask)))
            continue;
        /* port ranges only apply to port-bearing protocols */
        if (k->proto == DP_IPPROTO_TCP || k->proto == DP_IPPROTO_UDP) {
            if (k->sport < r->sport_lo || k->sport > r->sport_hi)
                continue;
            if (k->dport < r->dport_lo || k->dport > r->dport_hi)
                continue;
        }
        if (rule_id) *rule_id = r->rule_id;
        if (egress)  *egress  = r->egress;
        return r->action;
    }
    if (rule_id) *rule_id = 0;
    if (egress)  *egress  = DP_EGRESS_NONE;
    return DP_DECISION_NOMATCH;                     /* caller applies default */
}

/* ------------------------------------------------------------------ */
/* flow cache: open-addressed, power-of-two, linear probe             */
/* ------------------------------------------------------------------ */
static uint32_t dp_flow_hash(const struct dp_flow_key *k)
{
    /* FNV-1a over the key bytes: stable across architectures because we feed
     * it native fields in a fixed order rather than raw struct memory. */
    uint32_t h = 2166136261u;
    const uint8_t bytes[18] = {
        (uint8_t)(k->ip_a), (uint8_t)(k->ip_a >> 8),
        (uint8_t)(k->ip_a >> 16), (uint8_t)(k->ip_a >> 24),
        (uint8_t)(k->ip_b), (uint8_t)(k->ip_b >> 8),
        (uint8_t)(k->ip_b >> 16), (uint8_t)(k->ip_b >> 24),
        (uint8_t)(k->port_a), (uint8_t)(k->port_a >> 8),
        (uint8_t)(k->port_b), (uint8_t)(k->port_b >> 8),
        k->proto, k->vsys, 0, 0, 0, 0
    };
    for (int i = 0; i < 14; i++) { h ^= bytes[i]; h *= 16777619u; }
    return h;
}

static int dp_key_eq(const struct dp_flow_key *a, const struct dp_flow_key *b)
{
    return a->ip_a == b->ip_a && a->ip_b == b->ip_b &&
           a->port_a == b->port_a && a->port_b == b->port_b &&
           a->proto == b->proto && a->vsys == b->vsys;
}

/* Normalize so both directions of a conversation share one entry. */
void dp_flow_key_from_tuple(struct dp_flow_key *k, const struct dp_tuple *t)
{
    memset(k, 0, sizeof(*k));
    int a_first = (t->src_ip < t->dst_ip) ||
                  (t->src_ip == t->dst_ip && t->sport <= t->dport);
    if (a_first) {
        k->ip_a = t->src_ip; k->port_a = t->sport;
        k->ip_b = t->dst_ip; k->port_b = t->dport;
    } else {
        k->ip_a = t->dst_ip; k->port_a = t->dport;
        k->ip_b = t->src_ip; k->port_b = t->sport;
    }
    k->proto = t->proto;
    k->vsys  = t->vsys;
}

int dp_flow_init(struct dp_flow_table *ft, uint32_t slots)
{
    uint32_t p = 1;
    while (p < slots) p <<= 1;
    ft->slots = p;
    ft->mask = p - 1;
    ft->count = 0;
    ft->ent = (struct dp_flow_ent *)calloc(p, sizeof(struct dp_flow_ent));
    return ft->ent ? DP_OK : DP_ERR_NOMEM;
}

void dp_flow_fini(struct dp_flow_table *ft)
{
    free(ft->ent);
    ft->ent = NULL;
    ft->slots = ft->mask = ft->count = 0;
}

void dp_flow_flush(struct dp_flow_table *ft)
{
    if (ft->ent)
        memset(ft->ent, 0, (size_t)ft->slots * sizeof(struct dp_flow_ent));
    ft->count = 0;
}

struct dp_flow_ent *dp_flow_lookup(struct dp_flow_table *ft,
                                   const struct dp_flow_key *k)
{
    uint32_t i = dp_flow_hash(k) & ft->mask;
    for (uint32_t probe = 0; probe <= ft->mask; probe++) {
        struct dp_flow_ent *e = &ft->ent[(i + probe) & ft->mask];
        if (!e->used)
            return NULL;
        if (dp_key_eq(&e->key, k))
            return e;
    }
    return NULL;
}

struct dp_flow_ent *dp_flow_insert(struct dp_flow_table *ft,
                                   const struct dp_flow_key *k)
{
    uint32_t i = dp_flow_hash(k) & ft->mask;
    for (uint32_t probe = 0; probe <= ft->mask; probe++) {
        struct dp_flow_ent *e = &ft->ent[(i + probe) & ft->mask];
        if (!e->used) {
            memset(e, 0, sizeof(*e));
            e->key = *k;
            e->used = 1;
            ft->count++;
            return e;
        }
        if (dp_key_eq(&e->key, k))
            return e;
    }
    return NULL;                                    /* table full */
}

/* ------------------------------------------------------------------ */
/* packet parse                                                       */
/* ------------------------------------------------------------------ */
int dp_parse(const uint8_t *pkt, uint32_t len, uint8_t vsys, struct dp_tuple *t)
{
    if (len < DP_ETH_HLEN + DP_IP4_MIN_HLEN)
        return DP_ERR_SHORT;

    uint16_t ethertype = (uint16_t)((pkt[12] << 8) | pkt[13]);
    uint32_t off = DP_ETH_HLEN;
    if (ethertype == DP_ETHERTYPE_VLAN) {           /* single 802.1Q tag */
        if (len < DP_ETH_HLEN + 4 + DP_IP4_MIN_HLEN)
            return DP_ERR_SHORT;
        ethertype = (uint16_t)((pkt[16] << 8) | pkt[17]);
        off += 4;
    }
    if (ethertype != DP_ETHERTYPE_IPV4)
        return DP_ERR_NOTIP;

    const uint8_t *ip = pkt + off;
    uint8_t ihl = (uint8_t)((ip[0] & 0x0f) * 4);
    if (ihl < DP_IP4_MIN_HLEN || off + ihl > len)
        return DP_ERR_SHORT;

    memset(t, 0, sizeof(*t));
    t->proto  = ip[9];
    /* IPv4 header is network byte order: build host value explicitly */
    t->src_ip = ((uint32_t)ip[12] << 24) | ((uint32_t)ip[13] << 16) |
                ((uint32_t)ip[14] << 8)  | (uint32_t)ip[15];
    t->dst_ip = ((uint32_t)ip[16] << 24) | ((uint32_t)ip[17] << 16) |
                ((uint32_t)ip[18] << 8)  | (uint32_t)ip[19];
    t->vsys = vsys;

    if (t->proto == DP_IPPROTO_TCP || t->proto == DP_IPPROTO_UDP) {
        const uint8_t *l4 = ip + ihl;
        if (off + ihl + 4 > len)
            return DP_ERR_SHORT;
        t->sport = (uint16_t)((l4[0] << 8) | l4[1]);
        t->dport = (uint16_t)((l4[2] << 8) | l4[3]);
        if (t->proto == DP_IPPROTO_TCP && off + ihl + 14 <= len)
            t->tcp_flags = l4[13];

        /* Where the payload starts and ends, for the inline analysis engines.
         *
         * END is the subtle half. `len` is what the wire handed us, and a
         * frame shorter than 60 bytes was PADDED to that minimum by the
         * sender's MAC. Feeding the padding to a scanner as if it were payload
         * is how a run of zeros becomes a "match" -- so prefer the IPv4 Total
         * Length, which describes the datagram rather than the frame. Trust it
         * only when it is plausible: at least the IP header, and inside the
         * bytes we actually captured. A truncated capture or a bogus header
         * therefore falls back to `len`, which is always safe to read.
         *
         * START depends on the L4 header length. TCP carries it in the top
         * nibble of byte 12, in 32-bit words; UDP's is fixed at 8. A data
         * offset below 5 words is illegal, so it is clamped to the 20-byte
         * minimum rather than trusted -- an attacker-chosen 0 would otherwise
         * point the scanners at the TCP header itself. */
        uint32_t end = len;
        uint32_t tot = ((uint32_t)ip[2] << 8) | (uint32_t)ip[3];
        if (tot >= ihl && off + tot <= len)
            end = off + tot;

        uint32_t hlen;
        if (t->proto == DP_IPPROTO_TCP) {
            if (off + ihl + 20 > len)
                return DP_OK;            /* no room for a TCP header */
            hlen = (uint32_t)(l4[12] >> 4) * 4u;
            if (hlen < 20u)
                hlen = 20u;
        } else {
            hlen = 8u;
        }
        if (off + ihl + hlen < end) {
            t->pay_off = off + ihl + hlen;
            t->pay_len = end - t->pay_off;
        }
    }
    return DP_OK;
}

/* ------------------------------------------------------------------ */
/* per-packet processing                                              */
/* ------------------------------------------------------------------ */
/*
 * Resolve an egress port by ROUTING when the policy rule did not pin one.
 *
 * dp_classify() returns an egress carried by the matched rule, which is
 * bump-in-the-wire forwarding: the rule says where the packet goes and the
 * destination address is never consulted. That is deliberately left alone --
 * a rule that names an egress still wins. Routing only fills the gap where a
 * rule allows the traffic without saying where it goes.
 *
 * Called on the cache-hit path as well as after classify, because a cached
 * flow whose egress was never pinned must still route; skipping it there
 * would route the first packet of a flow and black-hole the rest.
 *
 * Failure handling is deliberate rather than convenient:
 *   no route     -> DROP. An unroutable packet must not fall through to a
 *                   default egress; guessing where to send it is worse than
 *                   dropping it.
 *   no neighbour -> LOCAL. The route exists but the next-hop MAC is unknown,
 *                   so punt to the local stack, which is what triggers ARP.
 *                   The packet is not lost, it is resolved and retried.
 */
static int dp_l3_resolve(struct dp_ctx *c, const struct dp_tuple *t,
                         struct dp_result *out)
{
    int rc;

    if (!c->l3 || out->egress != DP_EGRESS_NONE)
        return out->decision;
    if (out->decision != FP_FORWARD_W && out->decision != FP_INSPECT_W)
        return out->decision;

    rc = dp_l3_lookup(c->l3, t->dst_ip, &out->nh);
    if (rc != DP_L3_OK) {
        c->stat_l3_noroute++;
        return FP_DROP_W;
    }
    out->egress = out->nh.egress;
    if (!out->nh.have_mac) {
        uint32_t elen = sizeof(out->emit);
        /* The FIB found a route but not a MAC. Ask for it: on this box the DP
         * IS the router, so nothing above will do it for us. Rate limiting
         * lives in dp_arp_request -- without it a single flow toward an
         * unreachable host becomes an ARP flood at line rate. */
        if (dp_arp_request(c->l3, out->nh.egress, out->nh.nexthop,
                           out->emit, &elen) == DP_ARP_TX_REQUEST) {
            out->emit_len = (uint8_t)elen;
            out->emit_egress = out->nh.egress;
        }
        c->stat_l3_noneigh++;
        return FP_LOCAL_W;
    }
    out->routed = 1;
    c->stat_l3_routed++;
    return out->decision;
}

/*
 * Run the inline analysis engines over one packet and turn their verdict into
 * a forwarding decision.
 *
 * Called only for a packet already decided FP_INSPECT_W, and only AFTER
 * dp_l3_resolve() -- which can turn an INSPECT into a DROP (no route) or a
 * LOCAL (no neighbour). Scanning before that point would spend the budget on
 * packets that were never going to be forwarded.
 *
 * DIRECTION, and what is not being claimed here.
 * dp_engine_ctx.direction is a first-class filter in the DLP engine, and this
 * passes DP_DIR_UNKNOWN. That is deliberate rather than an oversight: the
 * question a direction-scoped rule asks is "is this leaving the protected
 * network", and nothing the forwarder holds answers it. The port table's
 * ffn_dp_port_raw.role classifies hardware (DATA / HA / MGMT / INTERNAL), not
 * trust, and dp_process is not even given the ingress port. Deriving a
 * direction from what IS available would be inventing the answer.
 *
 * The consequence is real and must be stated: a rule written "egress only"
 * fires in BOTH directions here, because the DLP engine skips a rule only when
 * both directions are known and disagree. Until a zone or trust attribute
 * reaches the port table, direction scoping is advisory.
 */
static int dp_inspect(struct dp_ctx *c, const uint8_t *pkt,
                      const struct dp_tuple *t, struct dp_result *out)
{
    struct dp_engine_ctx ec;
    int v;

    if (!c->engines || t->pay_len == 0)
        return FP_INSPECT_W;

    memset(&ec, 0, sizeof(ec));
    ec.payload     = pkt + t->pay_off;
    ec.payload_len = t->pay_len;
    ec.direction   = DP_DIR_UNKNOWN;
    ec.l4_proto    = t->proto;
    ec.dport       = t->dport;

    c->stat_engine_scanned++;
    v = dp_engine_scan(c->engines, &ec);

    out->engine_verdict = (uint8_t)v;
    out->engine_offset  = ec.hit_offset;
    out->engine_name    = ec.hit_engine;
    out->engine_rule    = ec.hit_rule;

    if (v == DP_EV_ALERT) {
        /* Report and pass. An alert that dropped the packet would be a block
         * under a friendlier name, and an operator who asked for visibility
         * would get an outage. */
        c->stat_engine_alert++;
        return FP_INSPECT_W;
    }
    if (v >= DP_EV_BLOCK) {
        c->stat_engine_block++;
        /* RESET additionally tears the flow down. The frame that would carry
         * the RST is not built here: dp_poll_once does not transmit
         * dp_result.emit today, so the ARP frames the L3 path already builds
         * are discarded too. Convicting the flow is the half that works; the
         * RST is the half that needs the emit path wired first. */
        out->reset = (v >= DP_EV_RESET);
        return FP_DROP_W;
    }
    return FP_INSPECT_W;
}

void dp_engine_attach(struct dp_ctx *c, struct dp_engine_set *set)
{
    if (c)
        c->engines = set;
}

int dp_process(struct dp_ctx *c, const uint8_t *pkt, uint32_t len,
               uint8_t vsys, struct dp_result *out)
{
    struct dp_tuple t;
    memset(out, 0, sizeof(*out));
    out->decision = c->default_decision;

    int rc = dp_parse(pkt, len, vsys, &t);
    if (rc != DP_OK) {
        /* Non-IPv4 / malformed: fail closed for malformed, pass ARP-like L2
         * to the local stack so neighbour discovery still works. */
        c->stat_parse_err++;
        /* ARP is not IPv4, so it lands here. Handle it rather than punting:
         * this is where neighbours are learned and where requests for our
         * own addresses are answered. */
        if (rc == DP_ERR_NOTIP && c->l3 && dp_arp_is_arp(pkt, len)) {
            uint32_t elen = sizeof(out->emit);
            uint16_t eg = 0;
            int arc = dp_arp_input(c->l3, pkt, len, out->emit, &elen, &eg);
            if (arc == DP_ARP_TX_REPLY) {
                out->emit_len = (uint8_t)elen;
                out->emit_egress = eg;
            }
            out->decision = FP_DROP_W;   /* consumed, not forwarded */
            return DP_OK;
        }
        out->decision = (rc == DP_ERR_NOTIP) ? FP_LOCAL_W : FP_DROP_W;
        return rc;
    }
    out->tuple = t;

    struct dp_flow_key key;
    dp_flow_key_from_tuple(&key, &t);

    struct dp_flow_ent *fe = dp_flow_lookup(&c->flows, &key);
    if (fe && fe->verdict != FP_V_UNSET_W) {
        out->from_cache = 1;
        out->rule_id = fe->rule_id;
        out->decision = (fe->verdict == FP_V_ALLOW_W)
                        ? (fe->flags & DP_FF_INSPECT ? FP_INSPECT_W : FP_FORWARD_W)
                        : FP_DROP_W;
        out->reset = (fe->verdict == FP_V_RESET_W);
        out->egress = fe->egress;
        fe->pkts++;
        fe->bytes += len;
        c->stat_cache_hit++;
        out->decision = dp_l3_resolve(c, &t, out);
        /* Analyse the head of the flow, not just its first packet -- and
         * budget by packets actually SCANNED, not by packets of the flow. A
         * TCP handshake plus a few bare ACKs carry no payload at all, so
         * counting flow packets would burn most of the budget before the first
         * byte of data arrived. Once a flow is convicted the verdict is
         * written back, so every later packet drops out of the cache without
         * scanning. */
        if (out->decision == FP_INSPECT_W && c->engines && t.pay_len &&
            fe->scans < DP_ENGINE_FLOW_PKTS) {
            fe->scans++;
            out->decision = dp_inspect(c, pkt, &t, out);
            if (out->decision == FP_DROP_W) {
                fe->verdict = (out->engine_verdict >= DP_EV_RESET)
                              ? FP_V_RESET_W : FP_V_DROP_W;
                fe->flags = (uint8_t)(fe->flags & ~DP_FF_INSPECT);
            }
        }
        /* Count the disposition on the cache path too: a firewall must report
         * every forwarded/dropped packet, not only the ones that reached
         * classify(), or the counters undercount steady-state traffic badly. */
        switch (out->decision) {
        case FP_FORWARD_W: c->stat_forward++; break;
        case FP_INSPECT_W: c->stat_inspect++; break;
        case FP_DROP_W:    c->stat_drop++;    break;
        default: break;
        }
        return DP_OK;
    }

    uint16_t rule_id = 0, egress = DP_EGRESS_NONE;
    int dec = dp_classify(&c->tables, &t, &rule_id, &egress);
    c->stat_classify++;
    if (dec == DP_DECISION_NOMATCH)
        dec = c->default_decision;

    out->decision = dec;
    out->rule_id = rule_id;
    out->egress = egress;
    out->decision = dec = dp_l3_resolve(c, &t, out);
    egress = out->egress;

    if (!fe)
        fe = dp_flow_insert(&c->flows, &key);
    if (fe) {
        fe->rule_id = rule_id;
        fe->egress = egress;
        fe->pkts++;
        fe->bytes += len;
    } else {
        c->stat_flow_full++;
    }

    /* Inline analysis. Ordered after routing, so it never scans a packet that
     * is about to be dropped for want of a route; after the flow entry exists,
     * so the per-flow scan budget can be enforced; and before the counter
     * switch below, which reads `dec` -- a conviction arriving later would be
     * counted as an inspect while the packet was dropped.
     *
     * Requiring `fe` is deliberate. With no flow entry there is nowhere to
     * keep the budget, so every packet of every flow would be scanned forever
     * -- and the case where there is no entry is precisely flow-table
     * exhaustion, which is when the box can least afford it. */
    if (dec == FP_INSPECT_W && fe && c->engines && t.pay_len &&
        fe->scans < DP_ENGINE_FLOW_PKTS) {
        fe->scans++;
        out->decision = dec = dp_inspect(c, pkt, &t, out);
    }

    if (fe) {
        switch (dec) {
        case FP_FORWARD_W: fe->verdict = FP_V_ALLOW_W; break;
        case FP_INSPECT_W: fe->verdict = FP_V_ALLOW_W;
                           fe->flags |= DP_FF_INSPECT; break;
        /* An engine RESET convicts the flow with FP_V_RESET_W, which the
         * cache-hit path above turns back into out->reset for every later
         * packet. Any other drop is a plain FP_V_DROP_W: engine_verdict is 0
         * unless the engines actually ran. */
        case FP_DROP_W:    fe->verdict = (out->engine_verdict >= DP_EV_RESET)
                                         ? FP_V_RESET_W : FP_V_DROP_W; break;
        default:           fe->verdict = FP_V_UNSET_W; break;  /* punt/local: re-evaluate */
        }
    }

    switch (dec) {
    case FP_FORWARD_W: c->stat_forward++; break;
    case FP_INSPECT_W: c->stat_inspect++; break;
    case FP_DROP_W:    c->stat_drop++;    break;
    case FP_PUNT_FPGA_W: c->stat_punt++;  break;
    case FP_LOCAL_W:   c->stat_local++;   break;
    default: break;
    }
    return DP_OK;
}

/* ------------------------------------------------------------------ */
/* shared-region control plane                                        */
/* ------------------------------------------------------------------ */
int dp_region_attach(struct dp_ctx *c, void *base, size_t size, int create)
{
    if (!base || size < FFN_DP_OFF_STATS)
        return DP_ERR_SHORT;
    c->region = base;
    c->region_size = size;
    if (create) {
        ffn_dp_hdr_init(base, DP_STATE_BOOT);
        ffn_dp_ring_init((uint8_t *)base + FFN_DP_OFF_CMD_RING);
        ffn_dp_ring_init((uint8_t *)base + FFN_DP_OFF_EVT_RING);
    }
    if (!ffn_dp_hdr_valid(base))
        return DP_ERR_HANDSHAKE;
    struct ffn_dp_hdr_raw *h = (struct ffn_dp_hdr_raw *)base;
    st_le32(h->dp_state, DP_STATE_HANDSHAKE);

    /* Advertise what this build can actually do. The MP gates port commands
     * on these bits, so getting them wrong in either direction is bad: too
     * few and port control is unusable, too many and the MP believes ports
     * are being programmed when nothing is driving hardware.
     *
     * PORT_HW is deliberately conditional -- without the CVMX accessors the
     * DP maintains the port table honestly but writes no registers, and the
     * MP needs to be able to see that. */
    {
        uint32_t caps = FFN_DP_CAP_PORT_CTL | FFN_DP_CAP_PORT_STATS;
#if defined(FFN_HAVE_CVMX)
        caps |= FFN_DP_CAP_PORT_HW;
#endif
        st_le32(h->dp_caps, caps);
    }
    return DP_OK;
}

void dp_set_state(struct dp_ctx *c, uint32_t state)
{
    if (!c->region) return;
    struct ffn_dp_hdr_raw *h = (struct ffn_dp_hdr_raw *)c->region;
    st_le32(h->dp_state, state);
}

uint32_t dp_get_state(struct dp_ctx *c)
{
    if (!c->region) return DP_STATE_RESET;
    const struct ffn_dp_hdr_raw *h = (const struct ffn_dp_hdr_raw *)c->region;
    return ld_le32(h->dp_state);
}

void dp_heartbeat(struct dp_ctx *c)
{
    if (!c->region) return;
    struct ffn_dp_hdr_raw *h = (struct ffn_dp_hdr_raw *)c->region;
    st_le64(h->dp_heartbeat, ld_le64(h->dp_heartbeat) + 1);
}

/* Activate a policy bank: parse the tables sitting in it. */
int dp_activate_bank(struct dp_ctx *c, uint32_t bank)
{
    if (!c->region || bank > 1)
        return DP_ERR_BANK;
    size_t off = (bank == 0) ? FFN_DP_OFF_BANK0 : FFN_DP_OFF_BANK1;
    if (off + FP_HDR_SIZE_W > c->region_size)
        return DP_ERR_SHORT;
    size_t avail = c->region_size - off;
    if (avail > FFN_DP_BANK_SIZE)
        avail = FFN_DP_BANK_SIZE;
    int rc = dp_tables_load(&c->tables, (uint8_t *)c->region + off, avail);
    if (rc != DP_OK)
        return rc;
    c->active_bank = bank;
    struct ffn_dp_hdr_raw *h = (struct ffn_dp_hdr_raw *)c->region;
    st_le32(h->active_bank, bank);
    /* A policy change invalidates cached verdicts. */
    dp_flow_flush(&c->flows);
    return DP_OK;
}

/* ------------------------------------------------------------------ */
/* Port control (ABI v2)                                              */
/*                                                                    */
/* Modelled on PAN's own port layer (libports.so on the 5220). Two    */
/* things about that model are worth keeping rather than flattening:  */
/*                                                                    */
/*  - the lifecycle is reset -> powerdown -> startup -> run, not a    */
/*    boolean, so a port that is admin-up but still training is       */
/*    distinguishable from one that is actually passing traffic;      */
/*  - media presence is a separate axis from link, because "no SFP    */
/*    fitted" and "SFP fitted but rejected" are different faults and  */
/*    an operator needs to tell them apart.                           */
/*                                                                    */
/* What this does NOT do: touch hardware. Programming a BGX LMAC      */
/* needs the CVMX register accessors, which are only compiled in      */
/* under -DFFN_HAVE_CVMX. Without them the DP maintains the port      */
/* table and reports honestly, and does not advertise                 */
/* FFN_DP_CAP_PORT_HW -- so the MP can see that nothing is really     */
/* being driven. dp_port_hw_apply() is the single hook where the      */
/* register writes belong.                                           */
/* ------------------------------------------------------------------ */

static void *dp_port_slot(struct dp_ctx *c, uint32_t lport)
{
    if (!c->region || lport >= FFN_DP_MAX_PORTS) return NULL;
    return ffn_dp_port(c->region, lport);
}

/* The one place hardware would be programmed. Returns DP_OK when the port
 * state was actually pushed to silicon, DP_ERR_UNSUPP when this build has no
 * register access. Deliberately not stubbed out to "success". */
static int dp_port_hw_apply(struct dp_ctx *c, struct ffn_dp_port_raw *p)
{
#if defined(FFN_HAVE_CVMX)
    /* Declared in ffn_dp_bgx.h, implemented in ffn_dp_bgx_octeon3.c. Kept out
     * of this file so the portable core stays free of chip headers. */
    /* BGX bring-up order matters: type and lane mapping first, then enable.
     * Enabling before LMAC_TYPE is set latches the wrong mode. */
    return dp_bgx_apply(c, p);
#else
    (void)c; (void)p;
    return DP_ERR_UNSUPP;
#endif
}


int dp_port_config(struct dp_ctx *c, uint32_t lport, uint64_t cfg, uint64_t a2)
{
    struct ffn_dp_port_raw *p = dp_port_slot(c, lport);
    if (!p) return DP_ERR_RANGE;

    uint8_t type = FFN_PORT_CFG_TYPE(cfg);
    if (type > FFN_PORT_TYPE_GHOST) return DP_ERR_RANGE;
    uint8_t neg = FFN_PORT_CFG_NEG(cfg);
    if (neg > FFN_PORT_NEG_FORCED) return DP_ERR_RANGE;
    uint8_t role = FFN_PORT_CFG_ROLE(cfg);
    if (role >= FFN_PORT_ROLE_MAX) return DP_ERR_RANGE;

    st_le16(p->lport, (uint16_t)lport);
    p->port_type   = type;
    p->lmac_type   = FFN_PORT_CFG_LMAC_TYPE(cfg);
    p->lane_to_sds = FFN_PORT_CFG_LANE(cfg);
    p->neg_mode    = neg;
    p->phy_addr    = FFN_PORT_CFG_PHY(cfg);
    p->flags       = FFN_PORT_CFG_FLAGS(cfg) | FFN_PORT_F_VALID;
    p->role        = role;
    st_le32(p->speed_mbps, FFN_PORT_A2_SPEED(a2));
    st_le32(p->mtu, FFN_PORT_A2_MTU(a2));

    /* Configuration alone must not start a port. PAN's flow goes through
     * powerdown before startup, and admin-up is a separate decision. */
    p->state    = FFN_PORT_ST_POWERDOWN;
    p->admin_up = 0;
    p->link_up  = 0;
    if (p->media == FFN_PORT_MEDIA_UNKNOWN)
        p->media = FFN_PORT_MEDIA_ABSENT;
    return DP_OK;
}

int dp_port_admin(struct dp_ctx *c, uint32_t lport, int up)
{
    struct ffn_dp_port_raw *p = dp_port_slot(c, lport);
    if (!p) return DP_ERR_RANGE;
    if (!(p->flags & FFN_PORT_F_VALID)) return DP_ERR_STATE;

    /* Never bring a non-data port up as a data port. Bridging two management
     * interfaces is the failure mode this platform already has a README about,
     * and the same applies to HA and HSCI: those carry cluster traffic, not
     * inspected traffic. The role axis decides, with the legacy MGMT flag
     * still honoured. */
    if (up && (p->flags & FFN_PORT_F_MGMT)) return DP_ERR_STATE;
    if (up && !ffn_port_role_bridgeable(p->role)) return DP_ERR_STATE;

    p->admin_up = up ? 1 : 0;
    if (up) {
        p->state = FFN_PORT_ST_STARTUP;
        int rc = dp_port_hw_apply(c, p);
        if (rc == DP_OK) p->state = FFN_PORT_ST_RUN;
        /* rc == DP_ERR_UNSUPP: stay in STARTUP. The port is not running and
         * the table says so, rather than claiming RUN with nothing behind it. */
    } else {
        p->state   = FFN_PORT_ST_POWERDOWN;
        p->link_up = 0;
    }
    return DP_OK;
}

static uint64_t dp_port_state_word(const struct ffn_dp_port_raw *p)
{
    return FFN_PORT_ST_PACK(p->port_type, p->state, p->neg_mode, p->media,
                            p->admin_up, p->link_up, p->flags, p->role);
}

int dp_port_count(struct dp_ctx *c)
{
    int n = 0;
    for (uint32_t i = 0; i < FFN_DP_MAX_PORTS; i++) {
        struct ffn_dp_port_raw *p = dp_port_slot(c, i);
        if (p && (p->flags & FFN_PORT_F_VALID)) n++;
    }
    return n;
}

/* Drain the MP->DP command ring. Returns commands handled. */
int dp_service_commands(struct dp_ctx *c)
{
    if (!c->region) return 0;
    void *cmd = (uint8_t *)c->region + FFN_DP_OFF_CMD_RING;
    void *evt = (uint8_t *)c->region + FFN_DP_OFF_EVT_RING;
    uint16_t op;
    uint64_t a0, a1, a2;
    int handled = 0;
    while (ffn_dp_ring_pop(cmd, &op, &a0, &a1, &a2)) {
        handled++;
        switch (op) {
        case DP_CMD_PING:
            ffn_dp_ring_push(evt, DP_EVT_PONG, a0, 0, 0);
            break;
        case DP_CMD_SET_BANK: {
            int rc = dp_activate_bank(c, (uint32_t)a0);
            if (rc == DP_OK) {
                dp_set_state(c, DP_STATE_READY);
                ffn_dp_ring_push(evt, DP_EVT_READY, a0, c->tables.policy_n, 0);
            } else {
                ffn_dp_ring_push(evt, DP_EVT_ERROR, (uint64_t)(-rc), a0, 0);
            }
            break;
        }
        case DP_CMD_SET_DEFAULT:
            c->default_decision = (int)a0;
            break;
        case DP_CMD_FLUSH_FLOWS:
            dp_flow_flush(&c->flows);
            break;
        case DP_CMD_GET_STATS:
            ffn_dp_ring_push(evt, DP_EVT_STATS, c->stat_forward, c->stat_drop,
                             c->stat_cache_hit);
            break;
        case DP_CMD_SHUTDOWN:
            c->stop = 1;
            break;
        case DP_CMD_PORT_ENUM:
            for (uint32_t i = 0; i < FFN_DP_MAX_PORTS; i++) {
                struct ffn_dp_port_raw *p = dp_port_slot(c, i);
                if (!p || !(p->flags & FFN_PORT_F_VALID)) continue;
                ffn_dp_ring_push(evt, DP_EVT_PORT_INFO, i,
                                 dp_port_state_word(p),
                                 FFN_PORT_A2_PACK(ld_le32(p->speed_mbps),
                                                  ld_le32(p->mtu)));
            }
            break;
        case DP_CMD_PORT_CONFIG: {
            int rc = dp_port_config(c, (uint32_t)a0, a1, a2);
            if (rc != DP_OK)
                ffn_dp_ring_push(evt, DP_EVT_ERROR, (uint64_t)(-rc), a0, 0);
            else {
                struct ffn_dp_port_raw *p = dp_port_slot(c, (uint32_t)a0);
                ffn_dp_ring_push(evt, DP_EVT_PORT_INFO, a0,
                                 dp_port_state_word(p), a2);
            }
            break;
        }
        case DP_CMD_PORT_ADMIN: {
            int rc = dp_port_admin(c, (uint32_t)a0, (int)a1);
            struct ffn_dp_port_raw *p = dp_port_slot(c, (uint32_t)a0);
            if (rc != DP_OK)
                ffn_dp_ring_push(evt, DP_EVT_ERROR, (uint64_t)(-rc), a0, 0);
            else
                ffn_dp_ring_push(evt, DP_EVT_PORT_LINK, a0, p->link_up,
                                 p->media);
            break;
        }
        case DP_CMD_PORT_STATS: {
            struct ffn_dp_port_raw *p = dp_port_slot(c, (uint32_t)a0);
            if (!p || !(p->flags & FFN_PORT_F_VALID))
                ffn_dp_ring_push(evt, DP_EVT_ERROR, DP_ERR_RANGE, a0, 0);
            else
                ffn_dp_ring_push(evt, DP_EVT_PORT_STATS, a0,
                                 ld_le64(p->rx_pkts), ld_le64(p->tx_pkts));
            break;
        }
        default:
            break;
        }
    }
    return handled;
}

/* ------------------------------------------------------------------ */
/* main loop                                                          */
/* ------------------------------------------------------------------ */
int dp_init(struct dp_ctx *c, const struct dp_io_ops *io, void *io_arg,
            uint32_t flow_slots)
{
    memset(c, 0, sizeof(*c));
    c->io = io;
    c->io_arg = io_arg;
    c->default_decision = FP_DROP_W;         /* fail closed */
    int rc = dp_flow_init(&c->flows, flow_slots ? flow_slots : DP_DEFAULT_FLOWS);
    if (rc != DP_OK)
        return rc;
    if (io && io->init)
        return io->init(io_arg);
    return DP_OK;
}

void dp_fini(struct dp_ctx *c)
{
    if (c->io && c->io->fini)
        c->io->fini(c->io_arg);
    dp_flow_fini(&c->flows);
}

/*
 * Advance the L3 clock and age the neighbour table.
 *
 * Nothing used to do this. l3->now_ms sat at 0 for the life of the process and
 * dp_arp_tick() had no caller anywhere outside its unit test, which looks like
 * a missing housekeeping call and is actually a forwarding bug.
 *
 * dp_arp_send_request() rate-limits retries on (now_ms - last_probe_ms). With
 * a frozen clock that difference is permanently 0, permanently below
 * DP_ARP_RETRY_MS, and so every retry is permanently suppressed: a neighbour
 * that missed the FIRST request -- a broadcast, exactly the frame most likely
 * to be dropped -- was never asked again. The INCOMPLETE entry recording that
 * failure was never reaped either, so the address stayed unresolved for the
 * life of the dataplane. One lost ARP broadcast blackholed a destination.
 *
 * Sampled once per burst rather than per packet, so the cost is spread across
 * up to DP_BURST frames. CLOCK_MONOTONIC, because a wall-clock step would
 * otherwise shift every neighbour timer at once.
 */
static void dp_l3_service(struct dp_ctx *c)
{
    struct timespec ts;
    uint32_t now;

    if (!c->l3)
        return;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return;            /* no clock: keep the last value, do not reset to 0 */

    now = (uint32_t)((uint64_t)ts.tv_sec * 1000u +
                     (uint64_t)ts.tv_nsec / 1000000u);
    c->l3->now_ms = now;

    /* Ageing walks the whole table, so keep it off the per-burst path. The
     * unsigned subtraction is deliberate: it stays correct across the ~49-day
     * wrap of a 32-bit millisecond counter. */
    if ((uint32_t)(now - c->l3_tick_ms) >= DP_ARP_TICK_MS) {
        c->l3_tick_ms = now;
        dp_arp_tick(c->l3);
    }
}

int dp_poll_once(struct dp_ctx *c)
{
    struct dp_pkt burst[DP_BURST];
    dp_service_commands(c);
    dp_heartbeat(c);
    dp_l3_service(c);

    if (!c->io || !c->io->rx)
        return 0;
    int n = c->io->rx(c->io_arg, burst, DP_BURST);
    for (int i = 0; i < n; i++) {
        struct dp_result res;
        c->stat_rx++;
        dp_process(c, burst[i].data, burst[i].len, burst[i].vsys, &res);
        burst[i].decision = res.decision;
        burst[i].egress = res.egress;
        if (res.decision == FP_FORWARD_W || res.decision == FP_INSPECT_W) {
            if (c->io->tx && c->io->tx(c->io_arg, &burst[i], 1) == 1)
                c->stat_tx++;
            else
                c->stat_tx_fail++;
        } else if (res.decision == FP_LOCAL_W) {
            if (c->io->to_local) c->io->to_local(c->io_arg, &burst[i]);
        } else if (res.decision == FP_PUNT_FPGA_W) {
            if (c->io->to_offload) c->io->to_offload(c->io_arg, &burst[i]);
        }
        if (c->io->free_pkt)
            c->io->free_pkt(c->io_arg, &burst[i]);
    }
    return n;
}

void dp_run(struct dp_ctx *c)
{
    dp_set_state(c, DP_STATE_READY);
    while (!c->stop)
        dp_poll_once(c);
    dp_set_state(c, DP_STATE_RESET);
}

const char *dp_strerror(int rc)
{
    switch (rc) {
    case DP_OK:            return "ok";
    case DP_ERR_SHORT:     return "buffer too short";
    case DP_ERR_MAGIC:     return "bad table magic";
    case DP_ERR_TYPE:      return "wrong table type";
    case DP_ERR_RECSZ:     return "unexpected record size";
    case DP_ERR_TOOMANY:   return "too many policy rows";
    case DP_ERR_NOMEM:     return "out of memory";
    case DP_ERR_NOTIP:     return "not IPv4";
    case DP_ERR_HANDSHAKE: return "shared-region handshake failed";
    case DP_ERR_BANK:      return "invalid policy bank";
    default:               return "unknown";
    }
}
