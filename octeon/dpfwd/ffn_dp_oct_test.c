/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_oct_test.c -- end-to-end test harness for the OCTEON-II dataplane.
 *
 * Proves the dataplane WITHOUT Octeon hardware: it builds real policy.bin bytes
 * in ffn_fastpath_compile.py's exact wire format (LE, 36-byte fp_hdr + 32-byte
 * rows), stages them into a simulated shared region, drives synthetic packets
 * through dp_process(), and exercises the MP<->DP command/event rings.
 *
 * Because the tables and the region are little-endian on the wire and the code
 * only touches them through the ld_leNN / st_leNN helpers, passing here on
 * x86-64 LE and on a big-endian build are the same test -- run it under
 * `qemu-mips64` (or on the Octeon) to validate BE with identical expectations.
 */
#include "ffn_dp_abi.h"
#include "ffn_dp_oct.h"
#include "ffn_dp_engine.h"
#include "ffn_dp_dlp.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_fail;
static void chk(int cond, const char *msg)
{
    printf("  %s %s\n", cond ? "ok  " : "FAIL", msg);
    if (!cond) g_fail++;
}

/* ---- build a policy.bin exactly as the Python compiler does ---- */
struct trow {
    uint32_t src_ip, src_mask, dst_ip, dst_mask;   /* host order in, NBO out */
    uint16_t sport_lo, sport_hi, dport_lo, dport_hi;
    uint8_t  proto, vsys, action, flags;
    uint16_t egress, rule_id;
};

static void put_nbo32(uint8_t *p, uint32_t host)
{
    /* IPv4 table fields are network-order bytes (what struct.pack("<I", ...) of
     * an already-NBO integer produces on the compiler side). */
    st_be32(p, host);
}

static size_t build_policy_bin(uint8_t *out, size_t cap,
                               const struct trow *rows, uint32_t n)
{
    size_t need = 36 + (size_t)n * 32;
    if (cap < need) return 0;
    memset(out, 0, need);
    memcpy(out, "FPPO", 4);
    st_le16(out + 4, 1);            /* version      */
    st_le16(out + 6, 0x40);         /* table_type   */
    st_le32(out + 8, n);            /* entry_count  */
    st_le64(out + 12, 1787000000ull); /* build_unix */
    st_le32(out + 20, 0);           /* crc32        */
    st_le32(out + 24, 32);          /* record_size  */
    st_le32(out + 28, n);           /* populated    */
    st_le32(out + 32, 0);           /* reserved     */
    uint8_t *r = out + 36;
    for (uint32_t i = 0; i < n; i++, r += 32) {
        put_nbo32(r + 0,  rows[i].src_ip);
        put_nbo32(r + 4,  rows[i].src_mask);
        put_nbo32(r + 8,  rows[i].dst_ip);
        put_nbo32(r + 12, rows[i].dst_mask);
        st_le16(r + 16, rows[i].sport_lo);
        st_le16(r + 18, rows[i].sport_hi);
        st_le16(r + 20, rows[i].dport_lo);
        st_le16(r + 22, rows[i].dport_hi);
        r[24] = rows[i].proto;
        r[25] = rows[i].vsys;
        r[26] = rows[i].action;
        r[27] = rows[i].flags;
        st_le16(r + 28, rows[i].egress);
        st_le16(r + 30, rows[i].rule_id);
    }
    return need;
}

/* ---- synthetic packet builder ---- */
static uint32_t mkpkt(uint8_t *buf, uint32_t sip, uint32_t dip, uint8_t proto,
                      uint16_t sp, uint16_t dp_, int vlan, uint32_t payload)
{
    uint32_t o = 0;
    memset(buf, 0, 128);
    memset(buf + 0, 0xAA, 6);          /* dst mac */
    memset(buf + 6, 0xBB, 6);          /* src mac */
    o = 12;
    if (vlan) {
        buf[o++] = 0x81; buf[o++] = 0x00;
        buf[o++] = 0x00; buf[o++] = 0x64;   /* vid 100 */
    }
    buf[o++] = 0x08; buf[o++] = 0x00;       /* IPv4 */
    uint8_t *ip = buf + o;
    ip[0] = 0x45;                            /* v4, ihl 5 */
    ip[9] = proto;
    ip[12] = (uint8_t)(sip >> 24); ip[13] = (uint8_t)(sip >> 16);
    ip[14] = (uint8_t)(sip >> 8);  ip[15] = (uint8_t)sip;
    ip[16] = (uint8_t)(dip >> 24); ip[17] = (uint8_t)(dip >> 16);
    ip[18] = (uint8_t)(dip >> 8);  ip[19] = (uint8_t)dip;
    uint8_t *l4 = ip + 20;
    l4[0] = (uint8_t)(sp >> 8); l4[1] = (uint8_t)sp;
    l4[2] = (uint8_t)(dp_ >> 8); l4[3] = (uint8_t)dp_;
    if (proto == DP_IPPROTO_TCP) {
        l4[12] = 0x50;                       /* data offset: 5 words = 20 B */
        l4[13] = 0x02;                       /* SYN */
    }
    /* IPv4 Total Length. Left zero until the payload locator started reading
     * it, which made every packet this builder produced look like a datagram
     * with no body -- a fixture that quietly disagreed with every real frame.
     * It describes the IP datagram, so it excludes the 14/18-byte L2 header
     * and, deliberately, any Ethernet padding a caller adds afterwards. */
    {
        uint32_t tot = 20 + 20 + payload;
        ip[2] = (uint8_t)(tot >> 8);
        ip[3] = (uint8_t)tot;
    }
    return o + 20 + 20 + payload;
}

/* Same frame with an actual payload copied in. Separate from mkpkt() so the
 * existing cases keep their exact bytes; buf must be at least 128 bytes, which
 * is what every caller here declares. */
static uint32_t mkpkt_data(uint8_t *buf, uint32_t sip, uint32_t dip,
                           uint8_t proto, uint16_t sp, uint16_t dp_, int vlan,
                           const char *body, uint32_t blen)
{
    uint32_t len = mkpkt(buf, sip, dip, proto, sp, dp_, vlan, blen);
    memcpy(buf + len - blen, body, blen);
    return len;
}

/* ---- sim I/O backend ---- */
struct sim_io {
    struct { uint8_t buf[128]; uint32_t len; uint8_t vsys; } q[64];
    int n, head;
    int txed, localed, offloaded;
};
static int sim_rx(void *arg, struct dp_pkt *b, int max)
{
    struct sim_io *s = (struct sim_io *)arg;
    int i = 0;
    while (i < max && s->head < s->n) {
        b[i].data = s->q[s->head].buf;
        b[i].len = s->q[s->head].len;
        b[i].vsys = s->q[s->head].vsys;
        b[i].cookie = NULL;
        s->head++; i++;
    }
    return i;
}
static int sim_tx(void *arg, struct dp_pkt *b, int n)
{
    (void)b; ((struct sim_io *)arg)->txed += n; return n;
}
static void sim_local(void *arg, struct dp_pkt *p)
{
    (void)p; ((struct sim_io *)arg)->localed++;
}
static void sim_offload(void *arg, struct dp_pkt *p)
{
    (void)p; ((struct sim_io *)arg)->offloaded++;
}
static const struct dp_io_ops SIM_IO = {
    "sim", NULL, NULL, sim_rx, sim_tx, sim_local, sim_offload, NULL
};
static void sim_push(struct sim_io *s, const uint8_t *pkt, uint32_t len, uint8_t vsys)
{
    if (s->n >= 64) return;
    memcpy(s->q[s->n].buf, pkt, len > 128 ? 128 : len);
    s->q[s->n].len = len;
    s->q[s->n].vsys = vsys;
    s->n++;
}

#define IP(a,b,c,d) (((uint32_t)(a)<<24)|((uint32_t)(b)<<16)|((uint32_t)(c)<<8)|(uint32_t)(d))

/* ---- ABI v2 port control ------------------------------------------------
 * Pinned against the model taken from PAN's libports.so: four lifecycle
 * states, negotiation and media as separate axes, and configuration that
 * does not by itself start a port. */
static void test_ports(void *region, struct dp_ctx *c)
{
    void *cmd = (uint8_t *)region + FFN_DP_OFF_CMD_RING;
    void *evt = (uint8_t *)region + FFN_DP_OFF_EVT_RING;
    uint16_t op; uint64_t a0, a1, a2;

    puts("");
    puts("[port] port table (ABI v2)");
    chk(FFN_DP_ABI_VER == 2, "ABI version is 2");
    chk(FFN_DP_OFF_PORTS + FFN_DP_PORTS_SIZE <= FFN_DP_OFF_BANK0,
        "port table fits before bank0 without overlapping it");

    /* the 5220's own complement: 2 RJ-45, 8 SFP+, 2 QSFP+ per Octeon */
    chk(dp_port_count(c) == 0, "no ports before configuration");

    uint64_t cfg = FFN_PORT_CFG_PACK(FFN_PORT_TYPE_SFP_PLUS, 3 /*10G-R*/,
                                     0x00, FFN_PORT_NEG_FORCED, 0xff,
                                     FFN_PORT_F_HAS_LMAC);
    ffn_dp_ring_push(cmd, DP_CMD_PORT_CONFIG, 0, cfg,
                     FFN_PORT_A2_PACK(10000, 9216));
    dp_service_commands(c);
    chk(dp_port_count(c) == 1, "one port after PORT_CONFIG");

    struct ffn_dp_port_raw *p = ffn_dp_port(region, 0);
    chk(p->port_type == FFN_PORT_TYPE_SFP_PLUS, "port type stored");
    chk(p->lmac_type == 3, "LMAC_TYPE 3 (10G-R) stored");
    chk(p->neg_mode == FFN_PORT_NEG_FORCED, "forced negotiation stored");
    chk(ld_le32(p->speed_mbps) == 10000, "speed stored");
    chk(ld_le32(p->mtu) == 9216, "mtu stored");
    chk(p->flags & FFN_PORT_F_VALID, "entry marked valid");

    /* the property that matters: configuring must not start it */
    chk(p->state == FFN_PORT_ST_POWERDOWN,
        "configuration leaves the port in POWERDOWN, not RUN");
    chk(p->admin_up == 0, "configuration does not imply admin-up");

    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2)) { }

    /* admin-up: without CVMX there is no hardware, so it must NOT claim RUN */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ADMIN, 0, 1, 0);
    dp_service_commands(c);
    chk(p->admin_up == 1, "admin-up recorded");
#if defined(FFN_HAVE_CVMX)
    chk(p->state == FFN_PORT_ST_RUN, "with CVMX the port reaches RUN");
#else
    chk(p->state == FFN_PORT_ST_STARTUP,
        "without CVMX the port stops at STARTUP rather than faking RUN");
#endif
    chk(ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2) && op == DP_EVT_PORT_LINK
        && a0 == 0, "admin change emits PORT_LINK");

    /* admin-down returns it to powerdown and clears link */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ADMIN, 0, 0, 0);
    dp_service_commands(c);
    chk(p->state == FFN_PORT_ST_POWERDOWN && p->link_up == 0,
        "admin-down powers the port down and clears link");
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2)) { }

    /* a management-class port must never come up as a data port */
    uint64_t mcfg = FFN_PORT_CFG_PACK(FFN_PORT_TYPE_RJ45, 0, 0,
                                      FFN_PORT_NEG_AUTONEG, 0x01,
                                      FFN_PORT_F_MGMT);
    ffn_dp_ring_push(cmd, DP_CMD_PORT_CONFIG, 1, mcfg,
                     FFN_PORT_A2_PACK(1000, 1500));
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ADMIN, 1, 1, 0);
    dp_service_commands(c);
    struct ffn_dp_port_raw *m = ffn_dp_port(region, 1);
    chk(m->admin_up == 0, "a management-class port refuses admin-up");
    int saw_err = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_ERROR && a1 == 1) saw_err = 1;
    chk(saw_err, "the refusal is reported as an error event");

    /* range and validity checks */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_CONFIG, FFN_DP_MAX_PORTS + 5, cfg, 0);
    dp_service_commands(c);
    saw_err = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_ERROR) saw_err = 1;
    chk(saw_err, "an out-of-range port id is refused");

    ffn_dp_ring_push(cmd, DP_CMD_PORT_ADMIN, 7, 1, 0);
    dp_service_commands(c);
    saw_err = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_ERROR) saw_err = 1;
    chk(saw_err, "admin on an unconfigured port is refused");

    /* enumeration reports exactly the configured ports */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ENUM, 0, 0, 0);
    dp_service_commands(c);
    int n_info = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_PORT_INFO) n_info++;
    chk(n_info == 2, "PORT_ENUM reports both configured ports");

    /* stats */
    st_le64(p->rx_pkts, 4242);
    st_le64(p->tx_pkts, 99);
    ffn_dp_ring_push(cmd, DP_CMD_PORT_STATS, 0, 0, 0);
    dp_service_commands(c);
    int ok_stats = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_PORT_STATS && a1 == 4242 && a2 == 99) ok_stats = 1;
    chk(ok_stats, "PORT_STATS returns the per-port counters");

    /* ---- role axis: form factor and purpose are orthogonal ----
     * A QSFP+ cage can be a data port or an HSCI (HA2/HA3) link, so the role
     * has to be carried separately -- conflating them was a real bug. */
    puts("");
    puts("[role] port role vs form factor");
    chk(ffn_port_role_bridgeable(FFN_PORT_ROLE_DATA), "DATA is bridgeable");
    chk(ffn_port_role_bridgeable(FFN_PORT_ROLE_AE), "AE is bridgeable");
    chk(!ffn_port_role_bridgeable(FFN_PORT_ROLE_HA), "HA is NOT bridgeable");
    chk(!ffn_port_role_bridgeable(FFN_PORT_ROLE_HSCI),
        "HSCI is NOT bridgeable -- it carries cluster traffic");
    chk(!ffn_port_role_bridgeable(FFN_PORT_ROLE_MGMT), "MGMT is NOT bridgeable");
    chk(!ffn_port_role_bridgeable(FFN_PORT_ROLE_INTERNAL),
        "INTERNAL is NOT bridgeable");

    /* an HSCI port: QSFP+ cage, 40G-R, but role HSCI */
    uint64_t hcfg = FFN_PORT_CFG_PACK_R(FFN_PORT_TYPE_QSFP_PLUS, 4 /*40G-R*/,
                                        0x00, FFN_PORT_NEG_FORCED, 0xff,
                                        FFN_PORT_F_HAS_LMAC,
                                        FFN_PORT_ROLE_HSCI);
    ffn_dp_ring_push(cmd, DP_CMD_PORT_CONFIG, 4, hcfg,
                     FFN_PORT_A2_PACK(40000, 9216));
    dp_service_commands(c);
    struct ffn_dp_port_raw *hp = ffn_dp_port(region, 4);
    chk(hp->role == FFN_PORT_ROLE_HSCI, "HSCI role stored");
    chk(hp->port_type == FFN_PORT_TYPE_QSFP_PLUS,
        "HSCI sits on a QSFP+ cage -- form factor kept separate from role");
    chk(hp->lmac_type == 4, "HSCI uses LMAC_TYPE 4 (40G-R)");
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2)) { }

    /* the safety property: HSCI must refuse to come up as a data port */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ADMIN, 4, 1, 0);
    dp_service_commands(c);
    chk(hp->admin_up == 0, "HSCI refuses admin-up as a data port");
    saw_err = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_ERROR && a1 == 4) saw_err = 1;
    chk(saw_err, "the HSCI refusal is reported as an error event");

    /* a DATA port on the same form factor still comes up */
    uint64_t dcfg = FFN_PORT_CFG_PACK_R(FFN_PORT_TYPE_QSFP_PLUS, 4, 0x01,
                                        FFN_PORT_NEG_FORCED, 0xff,
                                        FFN_PORT_F_HAS_LMAC,
                                        FFN_PORT_ROLE_DATA);
    ffn_dp_ring_push(cmd, DP_CMD_PORT_CONFIG, 5, dcfg,
                     FFN_PORT_A2_PACK(40000, 9216));
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ADMIN, 5, 1, 0);
    dp_service_commands(c);
    chk(ffn_dp_port(region, 5) &&
        ((struct ffn_dp_port_raw *)ffn_dp_port(region, 5))->admin_up == 1,
        "a DATA port on the same QSFP+ form factor does come up");
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2)) { }

    /* an out-of-range role is refused rather than silently coerced */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_CONFIG, 6,
                     FFN_PORT_CFG_PACK_R(FFN_PORT_TYPE_RJ45, 0, 0, 0, 0xff, 0,
                                         FFN_PORT_ROLE_MAX + 3),
                     0);
    dp_service_commands(c);
    saw_err = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_ERROR) saw_err = 1;
    chk(saw_err, "an unknown role is refused");

    /* role must survive the PORT_INFO round trip */
    ffn_dp_ring_push(cmd, DP_CMD_PORT_ENUM, 0, 0, 0);
    dp_service_commands(c);
    int hsci_seen = 0;
    while (ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2))
        if (op == DP_EVT_PORT_INFO && a0 == 4
            && ((a1 >> 48) & 0xff) == FFN_PORT_ROLE_HSCI) hsci_seen = 1;
    chk(hsci_seen, "PORT_INFO reports the HSCI role back to the MP");

    /* caps must not advertise hardware we do not have */
#if !defined(FFN_HAVE_CVMX)
    {
        const struct ffn_dp_hdr_raw *h =
            (const struct ffn_dp_hdr_raw *)region;
        chk(!(ld_le32(h->dp_caps) & FFN_DP_CAP_PORT_HW),
            "PORT_HW capability is not advertised without CVMX");
    }
#endif
}

int main(void)
{
    printf("=== FFN OCTEON-II dataplane test ===\n");
    printf("[host is %s-endian]\n",
           (*(const uint16_t *)"\x01\x02" == 0x0201) ? "little" : "big");

    /* ---------- 1. table load ---------- */
    printf("\n[1] policy table load (compiler wire format)\n");
    struct trow rows[] = {
      /* allow 10.1.0.0/16 -> any tcp/443, vsys1, rule 101 */
      { IP(10,1,0,0), IP(255,255,0,0), 0, 0, 0,65535, 443,443,
        DP_IPPROTO_TCP, 1, FP_FORWARD_W, 0, 7, 101 },
      /* inspect 10.1.0.0/16 -> any tcp/80 */
      { IP(10,1,0,0), IP(255,255,0,0), 0, 0, 0,65535, 80,80,
        DP_IPPROTO_TCP, 1, FP_INSPECT_W, 0, 7, 102 },
      /* drop anything to 192.0.2.66/32 */
      { 0, 0, IP(192,0,2,66), IP(255,255,255,255), 0,65535, 0,65535,
        0, 1, FP_DROP_W, 0, DP_EGRESS_NONE, 103 },
      /* udp/53 from any -> local */
      { 0, 0, 0, 0, 0,65535, 53,53, DP_IPPROTO_UDP, 1, FP_LOCAL_W, 0,
        DP_EGRESS_NONE, 104 },
      /* vsys2-only rule: proves vsys isolation */
      { IP(10,1,0,0), IP(255,255,0,0), 0, 0, 0,65535, 443,443,
        DP_IPPROTO_TCP, 2, FP_DROP_W, 0, DP_EGRESS_NONE, 105 },
    };
    uint32_t nrows = (uint32_t)(sizeof(rows) / sizeof(rows[0]));
    static uint8_t polbin[8192];
    size_t polsz = build_policy_bin(polbin, sizeof(polbin), rows, nrows);
    chk(polsz == 36 + nrows * 32, "policy.bin built at expected size");

    struct dp_tables t;
    int rc = dp_tables_load(&t, polbin, polsz);
    chk(rc == DP_OK, "dp_tables_load OK");
    chk(t.policy_n == nrows, "row count parsed");
    chk(t.policy[0].src_ip == IP(10,1,0,0), "row0 src_ip decoded to host order");
    chk(t.policy[0].src_mask == IP(255,255,0,0), "row0 mask decoded");
    chk(t.policy[0].dport_lo == 443 && t.policy[0].rule_id == 101,
        "row0 ports/rule_id decoded");
    chk(t.policy[2].dst_ip == IP(192,0,2,66), "row2 dst_ip decoded");

    /* rejection paths */
    uint8_t bad[64];
    memcpy(bad, polbin, 64);
    bad[0] = 'X';
    chk(dp_tables_load(&t, bad, 64) == DP_ERR_MAGIC, "bad magic rejected");
    memcpy(bad, polbin, 64);
    st_le32(bad + 24, 31);
    chk(dp_tables_load(&t, bad, 64) == DP_ERR_RECSZ, "bad record_size rejected");
    chk(dp_tables_load(&t, polbin, 20) == DP_ERR_SHORT, "truncated blob rejected");
    rc = dp_tables_load(&t, polbin, polsz);   /* reload good */

    /* ---------- 2. classify ---------- */
    printf("\n[2] classify\n");
    struct dp_tuple k;
    uint16_t rid, egr;
    memset(&k, 0, sizeof(k));
    k.src_ip = IP(10,1,2,3); k.dst_ip = IP(93,184,216,34);
    k.sport = 40000; k.dport = 443; k.proto = DP_IPPROTO_TCP; k.vsys = 1;
    chk(dp_classify(&t, &k, &rid, &egr) == FP_FORWARD_W && rid == 101,
        "vsys1 tcp/443 -> FORWARD rule 101");
    chk(egr == 7, "egress port carried from the rule");

    k.dport = 80;
    chk(dp_classify(&t, &k, &rid, NULL) == FP_INSPECT_W && rid == 102,
        "vsys1 tcp/80 -> INSPECT rule 102");

    k.dport = 443; k.vsys = 2;
    chk(dp_classify(&t, &k, &rid, NULL) == FP_DROP_W && rid == 105,
        "same 5-tuple in vsys2 -> DROP (vsys isolation)");

    k.vsys = 1; k.dst_ip = IP(192,0,2,66); k.dport = 8080;
    chk(dp_classify(&t, &k, &rid, NULL) == FP_DROP_W && rid == 103,
        "dst 192.0.2.66 -> DROP rule 103");

    k.dst_ip = IP(8,8,8,8); k.proto = DP_IPPROTO_UDP; k.dport = 53;
    chk(dp_classify(&t, &k, &rid, NULL) == FP_LOCAL_W && rid == 104,
        "udp/53 -> LOCAL rule 104");

    k.proto = DP_IPPROTO_TCP; k.dport = 9999; k.dst_ip = IP(1,1,1,1);
    chk(dp_classify(&t, &k, &rid, NULL) == DP_DECISION_NOMATCH,
        "no rule -> NOMATCH (caller applies default)");

    /* ---------- 3. parse ---------- */
    printf("\n[3] packet parse\n");
    uint8_t pkt[128];
    uint32_t len = mkpkt(pkt, IP(10,1,2,3), IP(93,184,216,34),
                         DP_IPPROTO_TCP, 40000, 443, 0, 0);
    struct dp_tuple pt;
    chk(dp_parse(pkt, len, 1, &pt) == DP_OK, "parse plain IPv4/TCP");
    chk(pt.src_ip == IP(10,1,2,3) && pt.dst_ip == IP(93,184,216,34),
        "addresses parsed to host order");
    chk(pt.sport == 40000 && pt.dport == 443, "ports parsed");
    chk(pt.tcp_flags == 0x02, "TCP flags parsed (SYN)");

    len = mkpkt(pkt, IP(10,1,2,3), IP(1,2,3,4), DP_IPPROTO_UDP, 1, 53, 1, 0);
    chk(dp_parse(pkt, len, 1, &pt) == DP_OK && pt.dport == 53,
        "802.1Q tagged frame parsed");
    uint8_t arp[64];
    memset(arp, 0, sizeof(arp));
    arp[12] = 0x08; arp[13] = 0x06;          /* ARP */
    chk(dp_parse(arp, 60, 1, &pt) == DP_ERR_NOTIP, "non-IPv4 reported");
    chk(dp_parse(pkt, 10, 1, &pt) == DP_ERR_SHORT, "runt rejected");

    /* ---------- 4. flow cache ---------- */
    printf("\n[4] flow cache (bidirectional normalization)\n");
    struct dp_flow_table ft;
    chk(dp_flow_init(&ft, 1024) == DP_OK, "flow table init");
    struct dp_tuple fwd = { IP(10,1,2,3), IP(5,6,7,8), 1234, 443,
                            DP_IPPROTO_TCP, 1, 0, 0, 0, 0 };
    struct dp_tuple rev = { IP(5,6,7,8), IP(10,1,2,3), 443, 1234,
                            DP_IPPROTO_TCP, 1, 0, 0, 0, 0 };
    struct dp_flow_key kf, kr;
    dp_flow_key_from_tuple(&kf, &fwd);
    dp_flow_key_from_tuple(&kr, &rev);
    chk(memcmp(&kf, &kr, sizeof(kf)) == 0,
        "forward and return direction map to ONE key");
    struct dp_flow_ent *e = dp_flow_insert(&ft, &kf);
    chk(e != NULL, "insert");
    e->verdict = FP_V_ALLOW_W; e->rule_id = 101;
    chk(dp_flow_lookup(&ft, &kr) == e, "return direction hits the same entry");
    struct dp_tuple other = fwd; other.vsys = 2;
    dp_flow_key_from_tuple(&kr, &other);
    chk(dp_flow_lookup(&ft, &kr) == NULL, "different vsys does not alias");
    dp_flow_flush(&ft);
    chk(ft.count == 0 && dp_flow_lookup(&ft, &kf) == NULL, "flush clears");
    dp_flow_fini(&ft);

    /* ---------- 5. shared region + rings ---------- */
    printf("\n[5] shared region, bank activation, command ring\n");
    size_t rsz = FFN_DP_OFF_BANK0 + 65536;
    uint8_t *region = (uint8_t *)calloc(1, rsz);
    struct dp_ctx ctx;
    struct sim_io sim;
    memset(&sim, 0, sizeof(sim));
    chk(dp_init(&ctx, &SIM_IO, &sim, 4096) == DP_OK, "dp_init");
    chk(ctx.default_decision == FP_DROP_W, "default decision is DROP (fail closed)");
    chk(dp_region_attach(&ctx, region, rsz, 1) == DP_OK, "region attach + handshake");
    chk(ffn_dp_hdr_valid(region), "region header magic/version valid");
    chk(dp_get_state(&ctx) == DP_STATE_HANDSHAKE, "state = HANDSHAKE");

    uint8_t junk[8] = {0};
    memcpy(region, junk, 8);
    chk(dp_region_attach(&ctx, region, rsz, 0) == DP_ERR_HANDSHAKE,
        "corrupt header fails the handshake (fail closed)");
    dp_region_attach(&ctx, region, rsz, 1);

    memcpy(region + FFN_DP_OFF_BANK0, polbin, polsz);
    void *cmd = region + FFN_DP_OFF_CMD_RING;
    void *evt = region + FFN_DP_OFF_EVT_RING;
    chk(ffn_dp_ring_push(cmd, DP_CMD_PING, 0xABCD, 0, 0) == 0, "push PING");
    chk(ffn_dp_ring_push(cmd, DP_CMD_SET_BANK, 0, 0, 0) == 0, "push SET_BANK 0");
    chk(dp_service_commands(&ctx) == 2, "DP handled 2 commands");
    chk(ctx.tables.policy_n == nrows, "bank activation loaded the tables");
    chk(dp_get_state(&ctx) == DP_STATE_READY, "state = READY after bank load");

    uint16_t op; uint64_t a0, a1, a2;
    chk(ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2) && op == DP_EVT_PONG &&
        a0 == 0xABCD, "PONG event with echoed cookie");
    chk(ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2) && op == DP_EVT_READY &&
        a1 == nrows, "READY event reports row count");
    chk(ffn_dp_ring_pop(evt, &op, &a0, &a1, &a2) == 0, "event ring drained");

    uint64_t hb = ld_le64(((struct ffn_dp_hdr_raw *)region)->dp_heartbeat);
    dp_heartbeat(&ctx);
    chk(ld_le64(((struct ffn_dp_hdr_raw *)region)->dp_heartbeat) == hb + 1,
        "heartbeat increments in the shared header");

    /* bad bank index must be refused */
    chk(dp_activate_bank(&ctx, 5) == DP_ERR_BANK, "invalid bank refused");

    /* ---------- 6. end-to-end forwarding ---------- */
    printf("\n[6] end-to-end packet processing\n");
    struct dp_result res;
    len = mkpkt(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                40000, 443, 0, 0);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_FORWARD_W && res.rule_id == 101 && !res.from_cache,
        "first packet: classified FORWARD (cache miss)");
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_FORWARD_W && res.from_cache,
        "second packet: served from the flow cache");

    len = mkpkt(pkt, IP(93,184,216,34), IP(10,1,2,3), DP_IPPROTO_TCP,
                443, 40000, 0, 0);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_FORWARD_W && res.from_cache,
        "return traffic hits the same cached flow");

    len = mkpkt(pkt, IP(10,9,9,9), IP(192,0,2,66), DP_IPPROTO_TCP,
                1111, 8080, 0, 0);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W && res.rule_id == 103,
        "blocked destination dropped");

    len = mkpkt(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                40001, 9999, 0, 0);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W,
        "unmatched traffic hits the DROP default (fail closed)");

    /* policy change must invalidate the cache */
    printf("\n[7] policy reload invalidates cached verdicts\n");
    struct trow rows2[] = {
      { IP(10,1,0,0), IP(255,255,0,0), 0, 0, 0,65535, 443,443,
        DP_IPPROTO_TCP, 1, FP_DROP_W, 0, DP_EGRESS_NONE, 201 },
    };
    polsz = build_policy_bin(polbin, sizeof(polbin), rows2, 1);
    memcpy(region + FFN_DP_OFF_BANK0, polbin, polsz);
    chk(dp_activate_bank(&ctx, 0) == DP_OK, "bank reactivated with new policy");
    chk(ctx.flows.count == 0, "flow cache flushed on policy change");
    len = mkpkt(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                40000, 443, 0, 0);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W && res.rule_id == 201,
        "previously-allowed flow now DROPs under the new policy");

    /* ---------- 8. poll loop through the sim backend ---------- */
    printf("\n[8] dp_poll_once through the I/O backend\n");
    polsz = build_policy_bin(polbin, sizeof(polbin), rows, nrows);
    memcpy(region + FFN_DP_OFF_BANK0, polbin, polsz);
    dp_activate_bank(&ctx, 0);
    memset(&sim, 0, sizeof(sim));
    len = mkpkt(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                40000, 443, 0, 0);
    sim_push(&sim, pkt, len, 1);                       /* -> forward */
    len = mkpkt(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                40002, 80, 0, 0);
    sim_push(&sim, pkt, len, 1);                       /* -> inspect (tx too) */
    len = mkpkt(pkt, IP(10,9,9,9), IP(192,0,2,66), DP_IPPROTO_TCP,
                1, 1, 0, 0);
    sim_push(&sim, pkt, len, 1);                       /* -> drop */
    len = mkpkt(pkt, IP(10,1,2,3), IP(8,8,8,8), DP_IPPROTO_UDP, 5, 53, 0, 0);
    sim_push(&sim, pkt, len, 1);                       /* -> local */
    int n = dp_poll_once(&ctx);
    chk(n == 4, "polled 4 packets");
    chk(sim.txed == 2, "2 transmitted (forward + inspect)");
    chk(sim.localed == 1, "1 delivered locally");
    chk(ctx.stat_drop >= 1, "drop counted");
    chk(ctx.stat_rx == 4, "rx counter");

    /* ---------- 9. inline analysis engines on the inspect path ---------- */
    printf("\n[9] inline analysis engines on the FP_INSPECT path\n");

    /* Locating the payload is the half of this that has to be right on a
     * big-endian target, so check it directly before checking any verdict.
     * Rule 102 sends 10.1.0.0/16 -> tcp/80 to FP_INSPECT_W with a pinned
     * egress, so routing does not enter into it. */
    static const char clean[] = "GET /index.html HTTP/1.1\r\nHost: example.com\r\n\r\n";
    static const char leak[]  = "card=4111111111111111&exp=12/29";
    const uint32_t clean_n = (uint32_t)(sizeof(clean) - 1);
    const uint32_t leak_n  = (uint32_t)(sizeof(leak) - 1);

    struct dp_tuple pl;
    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40010, 80, 0, clean, clean_n);
    chk(dp_parse(pkt, len, 1, &pl) == DP_OK && pl.pay_len == clean_n &&
        memcmp(pkt + pl.pay_off, clean, clean_n) == 0,
        "payload located exactly (no VLAN)");

    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40010, 80, 1, clean, clean_n);
    chk(dp_parse(pkt, len, 1, &pl) == DP_OK && pl.pay_len == clean_n &&
        memcmp(pkt + pl.pay_off, clean, clean_n) == 0,
        "payload located exactly through an 802.1Q tag");

    /* The case that makes IP Total Length the right end marker rather than the
     * captured length: a real MAC pads anything under 60 bytes, and offering
     * that padding to a scanner is how a run of zeros becomes a finding. */
    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40010, 80, 0, "hi", 2);
    {
        uint32_t padded = len;
        while (padded < 60) pkt[padded++] = 0;
        chk(dp_parse(pkt, padded, 1, &pl) == DP_OK && pl.pay_len == 2 &&
            memcmp(pkt + pl.pay_off, "hi", 2) == 0,
            "Ethernet padding is not offered to the engines as payload");
    }

    /* An illegal TCP data offset must not aim the scanners at the TCP header. */
    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40010, 80, 0, clean, clean_n);
    pkt[14 + 20 + 12] = 0x00;                     /* doff 0: below the minimum */
    chk(dp_parse(pkt, len, 1, &pl) == DP_OK && pl.pay_len == clean_n,
        "an illegal TCP data offset is clamped to the 20-byte minimum");

    /* EVASION: understate IPv4 Total Length and carry data behind it. The
     * forwarder transmits the whole captured frame either way, so a scanner
     * that stopped at the declared length would see nothing while every byte
     * went out on the wire. */
    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40010, 80, 0, leak, leak_n);
    pkt[14 + 2] = 0x00; pkt[14 + 3] = 40;      /* claim a bare 40-byte datagram */
    chk(dp_parse(pkt, len, 1, &pl) == DP_OK && pl.pay_len == leak_n &&
        memcmp(pkt + pl.pay_off, leak, leak_n) == 0,
        "an understated IPv4 Total Length does not hide payload from the engines");


    /* With no engine set attached the forwarder must behave exactly as before.
     * This is the assertion that says linking the engines in changed nothing by
     * itself; enabling them is a separate, explicit act. */
    dp_activate_bank(&ctx, 0);
    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40010, 80, 0, leak, leak_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_INSPECT_W && res.engine_verdict == DP_EV_NONE,
        "no engine set attached: INSPECT unchanged even on a hit payload");

    struct dp_engine_set eng;
    struct dp_dlp dlp;
    memset(&eng, 0, sizeof(eng));
    memset(&dlp, 0, sizeof(dlp));
    chk(dp_engine_register(&eng, "dlp", dp_dlp_scan, &dlp) >= 0,
        "DLP engine registered");
    chk(dp_dlp_config_line(&dlp, "dp.dlp.rule.pan", "credit_card:block:any:") == 1,
        "credit-card block rule accepted from config");
    chk(dp_engine_enable(&eng, "dlp", 1) == 0, "engine enabled");
    dp_engine_attach(&ctx, &eng);
    dp_activate_bank(&ctx, 0);                    /* flush the flow cache */

    len = mkpkt_data(pkt, IP(10,1,2,3), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40011, 80, 0, clean, clean_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_INSPECT_W && res.engine_verdict == DP_EV_NONE,
        "clean payload scanned and left as INSPECT");

    len = mkpkt_data(pkt, IP(10,1,2,4), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40012, 80, 0, leak, leak_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W && res.engine_verdict == DP_EV_BLOCK &&
        res.engine_name && strcmp(res.engine_name, "dlp") == 0,
        "card number turns INSPECT into DROP, and names the engine");
    chk(ctx.stat_engine_block == 1,
        "the conviction is counted as an engine block");

    /* The conviction has to stick to the FLOW, or the next packet of the same
     * connection walks through carrying the rest of the data. */
    len = mkpkt_data(pkt, IP(10,1,2,4), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40012, 80, 0, clean, clean_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W && res.from_cache,
        "convicted flow keeps dropping from the cache, clean payload or not");

    /* Head-of-flow budget: the engines see the first DP_ENGINE_FLOW_PKTS
     * packets of a flow and then stop, so a long-lived flow cannot make the
     * forwarder scan forever. */
    {
        uint64_t before = ctx.stat_engine_scanned;
        for (unsigned i = 0; i < DP_ENGINE_FLOW_PKTS + 6u; i++) {
            len = mkpkt_data(pkt, IP(10,1,2,5), IP(93,184,216,34),
                             DP_IPPROTO_TCP, 40013, 80, 0, clean, clean_n);
            dp_process(&ctx, pkt, len, 1, &res);
        }
        chk(ctx.stat_engine_scanned - before == DP_ENGINE_FLOW_PKTS,
            "engines see the head of a flow, then stop");
    }

    /* RESET is a stronger conviction than BLOCK and has to reach the flow entry
     * as FP_V_RESET_W, which is what makes out->reset true on later packets. */
    memset(&dlp, 0, sizeof(dlp));
    chk(dp_dlp_config_line(&dlp, "dp.dlp.rule.pan2",
                           "credit_card:reset:any:") == 1, "reset rule accepted");
    dp_activate_bank(&ctx, 0);
    len = mkpkt_data(pkt, IP(10,1,2,6), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40014, 80, 0, leak, leak_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W && res.reset &&
        res.engine_verdict == DP_EV_RESET, "reset rule drops and asks for RST");
    len = mkpkt_data(pkt, IP(10,1,2,6), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40014, 80, 0, clean, clean_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_DROP_W && res.from_cache && res.reset,
        "the cached conviction keeps asking for RST");

    /* Detaching must restore the previous behaviour exactly. */
    dp_engine_attach(&ctx, NULL);
    dp_activate_bank(&ctx, 0);
    len = mkpkt_data(pkt, IP(10,1,2,7), IP(93,184,216,34), DP_IPPROTO_TCP,
                     40015, 80, 0, leak, leak_n);
    chk(dp_process(&ctx, pkt, len, 1, &res) == DP_OK &&
        res.decision == FP_INSPECT_W,
        "detaching the engine set restores plain INSPECT");

    /* The budget counts packets SCANNED, not packets of the flow. A handshake
     * and a few bare ACKs carry no payload; if they spent the budget, the
     * first byte of real data could arrive with nothing left to scan it. */
    dp_engine_attach(&ctx, &eng);
    dp_activate_bank(&ctx, 0);
    {
        uint64_t before = ctx.stat_engine_scanned;
        for (int i = 0; i < 6; i++) {
            len = mkpkt(pkt, IP(10,1,2,8), IP(93,184,216,34), DP_IPPROTO_TCP,
                        40016, 80, 0, 0);            /* no payload at all */
            dp_process(&ctx, pkt, len, 1, &res);
        }
        chk(ctx.stat_engine_scanned == before,
            "payload-free packets do not consume the scan budget");
        for (unsigned i = 0; i < DP_ENGINE_FLOW_PKTS + 4u; i++) {
            len = mkpkt_data(pkt, IP(10,1,2,8), IP(93,184,216,34),
                             DP_IPPROTO_TCP, 40016, 80, 0, clean, clean_n);
            dp_process(&ctx, pkt, len, 1, &res);
        }
        chk(ctx.stat_engine_scanned - before == DP_ENGINE_FLOW_PKTS,
            "the full budget is still available once data arrives");
    }
    dp_engine_attach(&ctx, NULL);


    printf("\ncounters: rx=%llu tx=%llu fwd=%llu insp=%llu drop=%llu local=%llu "
           "cache_hit=%llu classify=%llu\n",
           (unsigned long long)ctx.stat_rx, (unsigned long long)ctx.stat_tx,
           (unsigned long long)ctx.stat_forward, (unsigned long long)ctx.stat_inspect,
           (unsigned long long)ctx.stat_drop, (unsigned long long)ctx.stat_local,
           (unsigned long long)ctx.stat_cache_hit,
           (unsigned long long)ctx.stat_classify);

    /* test_ports() reads the port table out of the shared region and uses the
     * live ctx, so it has to run while both still exist. It was called after
     * dp_fini() and free(region) -- a use-after-free that happened to pass
     * because glibc had not yet reused the pages. */
    test_ports(region, &ctx);
    dp_fini(&ctx);
    free(region);

    printf("\n==== ffn_dp_oct test: %d failed ====\n", g_fail);
    return g_fail ? 1 : 0;
}
