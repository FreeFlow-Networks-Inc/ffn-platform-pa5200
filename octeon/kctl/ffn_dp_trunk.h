/* SPDX-License-Identifier: GPL-2.0-only */
/* PA-5220 CN78XX raw internal trunk: the one owner of PKI, SSO and PKO on the
 * dataplane. Included after ffn_dp_dma.h. Register layouts and command
 * formats checked against the operator's SDK 5.1 cvmx-pki, cvmx-pko3 and
 * octeon3-ethernet driver. No SDK allocator is used.
 *
 * One PKND. Receive runs on the kernel's SSO groups, 0..rx_groups-1, one
 * pinned thread each: the classifier hashes every frame to a flow tag whose
 * low bits pick the group, so a flow stays in order on one core while flows
 * spread. The kernel threads only ever GET_WORK on those groups; data traffic
 * can be steered to an application's groups at runtime (`steer app`), and a
 * userspace dataplane punts to the kernel by adding work to a kernel group
 * with the same entry and buffer layout (octeon/PORT-NETDEVS.md has the
 * contract). Transmit uses the kernel's PKO descriptor queues 0..tx_queues-1,
 * one per transmit queue of every netdev, chosen by the sending CPU.
 *
 * Netdevs: ffnpkt0 carries the FE100/BCM envelope raw over AF_PACKET as it
 * always has. With port_netdevs=1 there is also one Ethernet netdev per
 * faceplate port (ffnp1..ffnp24), one per LAG (ffnae<n>) and ffnret0 for the
 * FE100's return messages: receive demultiplexes by the source system port
 * with the four-byte header stripped, transmit prepends the ITMH for the
 * port's destination system port. The control file selects what receives
 * what (`deliver trunk|ports|both`). */
#include <linux/etherdevice.h>
#include <linux/kthread.h>
#include <linux/log2.h>
#include <linux/rtnetlink.h>
#include <linux/socket.h>
#include <linux/smp.h>
#include <asm/octeon/cvmx-scratch.h>
#include <asm/octeon/cvmx-bgxx-defs.h>

#define FFN_TRUNK_MAX 3584
#define FFN_TX_SLOTS 64
#define FFN_PHYS_MASK ((1ULL << 42) - 1)
#define FFN_RX_GROUPS_MAX FFN_KERNEL_GROUPS_MAX
#define FFN_PKO_WORDS_MAX 6
#define FFN_TX_PAD_OFFSET 64 /* zero bytes in the envelope buffer, after the 24-byte envelope */
#define FFN_TX_DONE_OFFSET (FFN_DMA_BLOCK - 8)
/* Receive groups and threads: a power of two, 1..8. The classifier adds that
 * many low tag bits to the SSO group. Read once, at trunk registration. */
static unsigned rx_groups = 8;
module_param(rx_groups, uint, 0444);
MODULE_PARM_DESC(rx_groups, "kernel SSO receive groups and threads, a power of two 1..8");
/* PKI_TAG_INC()_MASK bit order: 0 = bit i names byte i of the window, 1 = bit 63
 * does. Applied at every trunk open, so it can be settled on the box by watching
 * one flow stay in one group. */
static unsigned tag_mask_msb;
module_param(tag_mask_msb, uint, 0644);
MODULE_PARM_DESC(tag_mask_msb, "tag mask bit 63 names the first byte of the window");
/* Per-port netdevs beside ffnpkt0. 0 keeps the trunk as it was. */
static unsigned port_netdevs;
module_param(port_netdevs, uint, 0444);
MODULE_PARM_DESC(port_netdevs, "register ffnp1..24, ffnae<n> and ffnret0 beside ffnpkt0 (0 = ffnpkt0 only)");
/* Port transmit: 1 gathers the payload straight from the skb (one 24-byte
 * envelope written into a pool buffer, the frame read by PKO where it is);
 * 0 copies the frame into the pool buffer first, the path proven on the box. */
static unsigned tx_gather;
module_param(tx_gather, uint, 0644);
MODULE_PARM_DESC(tx_gather, "port netdev transmit by gather from the skb (1) or by one copy (0)");

static struct net_device *trunk;
static struct task_struct *trunk_workers[FFN_RX_GROUPS_MAX];
static unsigned trunk_groups; /* in use after registration */
static bool trunk_running, trunk_accepting, trunk_configured;
static int trunk_error;
static const char *trunk_stage = "unprepared";
/* Engine-wide receive counters: each group has one writer, its own thread. */
static u64 trunk_rx_group[FFN_RX_GROUPS_MAX], trunk_rx_bytes_group[FFN_RX_GROUPS_MAX],
           trunk_rx_drop_group[FFN_RX_GROUPS_MAX], trunk_mismatch_group[FFN_RX_GROUPS_MAX],
           trunk_unmapped_group[FFN_RX_GROUPS_MAX], trunk_punt_tagged_group[FFN_RX_GROUPS_MAX];
static u64 trunk_bad_dma;
static unsigned trunk_cpu_group[FFN_RX_GROUPS_MAX]; /* the CPU each group's thread runs on */
static u64 trunk_last_ack;

/* One PKO descriptor queue: its lock, the frames in flight and its counters. */
struct ffn_txq {
    spinlock_t lock;
    struct {
        int index;              /* pool buffer, -1 when the slot is free */
        struct sk_buff *skb;    /* gathered from the skb: released at completion */
        unsigned long submitted;
    } pending[FFN_TX_SLOTS];
    u64 tx, tx_bytes, done, drop;
    bool open;
};
static struct ffn_txq trunk_txq[FFN_TXQ_MAX];

/* A netdev of the driver and its counters: ffnpkt0 and ffnret0 (raw), the
 * faceplate ports and the LAGs. Receive counters are per group (one writer
 * each), transmit counters per descriptor queue (under its lock). */
struct ffn_port {
    struct net_device *dev;
    unsigned number;            /* faceplate 1..24, LAG id 1..8, 0 for the raw netdevs */
    bool lag, raw;
    u16 sysport;                /* transmit destination; receive source. LAG: 0x8000|tid */
    unsigned tid;
    u32 member_mask, collect, distribute; /* LAG: bit = faceplate port - 1 */
    bool hardware;              /* LAG: transmit to the LAG system port with a key */
    struct ffn_port *member_of; /* a port that belongs to a LAG */
    u64 rx[FFN_RX_GROUPS_MAX], rx_bytes[FFN_RX_GROUPS_MAX], rx_drop[FFN_RX_GROUPS_MAX];
    u64 rx_down[FFN_RX_GROUPS_MAX]; /* demultiplexed here while the netdev was down */
    u64 tx[FFN_TXQ_MAX], tx_bytes[FFN_TXQ_MAX], tx_drop[FFN_TXQ_MAX];
};
static struct ffn_port trunk_self, trunk_return, trunk_port[FFN_FACEPLATE_PORTS], trunk_lag[FFN_LAGS_MAX];
static struct ffn_port *trunk_rx_map[FFN_SYSPORT_MAP]; /* by source system port; written under init_lock */
static unsigned trunk_return_port = FFN_PORT_RETURN_DEFAULT;
static enum ffn_deliver trunk_deliver = FFN_DELIVER_TRUNK;
static bool trunk_steer_app;
static unsigned trunk_app_base, trunk_app_log2;
static u64 trunk_ctl_commands, trunk_ctl_rejected;

static u64 trunk_sum(const u64 *values, unsigned n)
{
    u64 total = 0;
    unsigned i;
    for (i = 0; i < n; i++)
        total += READ_ONCE(values[i]);
    return total;
}

static struct ffn_port *port_of(struct net_device *dev)
{
    return *(struct ffn_port **)netdev_priv(dev);
}

/* Exact allocation membership, including range overflow, before any DMA
 * pointer is dereferenced or returned to FPA. FPA start/end alone is not enough
 * because dma_alloc_coherent blocks need not be adjacent. */
static int trunk_buffer(u64 address, unsigned bytes)
{
    struct ffn_dma_pool *p = &dma_pools[0];
    unsigned i;
    for (i = 0; i < p->count; i++)
        if (address >= p->buffer[i].dma && bytes <= FFN_DMA_BLOCK &&
            address - p->buffer[i].dma <= FFN_DMA_BLOCK - bytes)
            return i;
    return -1;
}

/* The running kernel reserves two CVMSEG lines. Temporarily use line 1,
 * preserving both scratch contents and CVMMEMCTL. SDK line 2 would be outside
 * this kernel's allocation. Interrupts and migration stay disabled through
 * the real IOBDMA acknowledgement; never manufacture a successful result.
 * Every CPU has its own CVMSEG, so queues may be driven concurrently. */
static u64 trunk_pko_command(unsigned op, const u64 *words, unsigned count, unsigned dq)
{
    unsigned long flags;
    u64 saved[FFN_PKO_WORDS_MAX + 1], ctl, result, command;
    unsigned i, ret = 128 + count * 8;
    preempt_disable();
    local_irq_save(flags);
    CVMX_SYNCIOBDMA;
    ctl = read_c0_cvmmemctl();
    write_c0_cvmmemctl((ctl & ~(63ULL << 45)) | (1ULL << 45) | (1ULL << 51));
    for (i = 0; i <= count; i++)
        saved[i] = cvmx_scratch_read64(128 + i * 8);
    for (i = 0; i < count; i++)
        cvmx_scratch_write64(128 + i * 8, words[i]);
    cvmx_scratch_write64(ret, ~0ULL);
    command = ffn_pko_lmtdma_word(ret, op, dq); /* node 0 */
    CVMX_SYNCWS;
    cvmx_write64_uint64(count ? 0xffffffffffffa400ULL + (count - 1) * 8 :
                                0xffffffffffffa200ULL, command);
    CVMX_SYNCIOBDMA;
    result = cvmx_scratch_read64(ret);
    for (i = 0; i <= count; i++)
        cvmx_scratch_write64(128 + i * 8, saved[i]);
    write_c0_cvmmemctl(ctl);
    local_irq_restore(flags);
    preempt_enable();
    return result;
}

#define TRUNK_CSR(r, v) do { rc = csr_checked((r), (v)); if (rc) return rc; } while (0)

static u64 trunk_tag_bytes(unsigned first, unsigned last)
{
    u64 mask = 0;
    unsigned i;
    for (i = first; i <= last; i++)
        mask |= 1ULL << (tag_mask_msb ? 63 - i : i);
    return mask;
}

/* Flow tags, applied at every open so tag_mask_msb can be settled on the box.
 * Byte windows are relative to the start of L2 parsing, after the four-byte
 * envelope: an untagged IPv4 frame has addresses and ports at 26..37, a
 * VLAN-tagged one at 30..41, and a return message's FLOWUNKNOWN key (protocol,
 * zone, ports, addresses) at 28..43. An IPv6 address or an exception's packet
 * info falls in the same windows and still hashes one flow to one group. */
static int trunk_tag_configure(void)
{
    static const struct { unsigned style, entry, first, last; } tags[] = {
        {8, 0, 26, 37}, {9, 4, 30, 41}, {10, 8, 28, 43}, {11, 8, 28, 43},
    };
    union cvmx_pki_tag_incx_ctl ctl = {.u64 = 0};
    union cvmx_pki_stylex_tag_sel sel = {.u64 = 0};
    unsigned i;
    int rc;
    ctl.s.ptr_sel = 8; /* relative to PKI_WQE_S[LAPTR] */
    for (i = 0; i < ARRAY_SIZE(tags); i++) {
        TRUNK_CSR(CVMX_PKI_TAG_INCX_CTL(tags[i].entry), ctl.u64);
        TRUNK_CSR(CVMX_PKI_TAG_INCX_MASK(tags[i].entry), trunk_tag_bytes(tags[i].first, tags[i].last));
        sel.s.tag_idx0 = tags[i].entry / 4;
        TRUNK_CSR(CVMX_PKI_STYLEX_TAG_SEL(tags[i].style), sel.u64);
    }
    return 0;
}

/* One parser CAM entry on every cluster, ternary fields as the SDK encodes them. */
static int trunk_pcam_entry(unsigned bank, unsigned index, unsigned term, unsigned style,
                            unsigned style_mask, u32 data, u32 data_mask, unsigned style_add, unsigned pmc)
{
    union cvmx_pki_clx_pcamx_termx t = {.u64 = 0};
    union cvmx_pki_clx_pcamx_matchx m = {.u64 = 0};
    union cvmx_pki_clx_pcamx_actionx a = {.u64 = 0};
    unsigned c;
    int rc;
    m.s.data1 = data & data_mask;
    m.s.data0 = ~data & data_mask;
    a.s.style_add = style_add;
    a.s.pmc = pmc;
    t.s.term1 = term & 0xff;
    t.s.term0 = ~term & 0xff;
    t.s.style1 = style & style_mask;
    t.s.style0 = ~style & style_mask;
    t.s.valid = 1;
    for (c = 0; c < 4; c++) {
        TRUNK_CSR(CVMX_PKI_CLX_PCAMX_MATCHX(c, bank, index), m.u64);
        TRUNK_CSR(CVMX_PKI_CLX_PCAMX_ACTIONX(c, bank, index), a.u64);
        TRUNK_CSR(CVMX_PKI_CLX_PCAMX_TERMX(c, bank, index), t.u64);
    }
    return 0;
}

/* The two QPG entries. Entry 8 is data (styles 8 untagged and 9 VLAN) and
 * follows the steering: the kernel's groups, or the application's. Entry 9
 * is the FE100's returns (styles 10 and 11) and always names the kernel's
 * groups, so ffnret0 keeps receiving whoever owns the data path. */
static int trunk_qpg_write(unsigned entry, unsigned group, unsigned tag_bits)
{
    union cvmx_pki_qpg_tblx qpg = {.u64 = 0};
    int rc;
    qpg.s.laura = dma_pools[0].aura;
    qpg.s.grp_ok = group;
    qpg.s.grp_bad = group;
    qpg.s.grptag_ok = tag_bits;
    qpg.s.grptag_bad = tag_bits;
    TRUNK_CSR(CVMX_PKI_QPG_TBLX(entry), qpg.u64);
    TRUNK_CSR(CVMX_PKI_QPG_TBLBX(entry), 0);
    return 0;
}

static int trunk_steer_apply(void)
{
    if (trunk_steer_app)
        return trunk_qpg_write(8, trunk_app_base, trunk_app_log2);
    return trunk_qpg_write(8, 0, ilog2(trunk_groups));
}

static int trunk_pki_configure(void)
{
    union cvmx_pki_icgx_cfg cluster;
    union cvmx_pki_clx_pkindx_style ps = {.u64 = 0};
    union cvmx_pki_clx_pkindx_cfg pc = {.u64 = 0};
    union cvmx_pki_clx_pkindx_skip skip = {.u64 = 0};
    union cvmx_pki_clx_stylex_cfg sc = {.u64 = 0};
    union cvmx_pki_clx_stylex_cfg2 cfg2 = {.u64 = 0};
    union cvmx_pki_clx_stylex_alg alg = {.u64 = 0};
    union cvmx_pki_stylex_buf buf = {.u64 = 0};
    union cvmx_pki_clx_pcamx_termx term;
    union cvmx_bgxx_cmrx_rx_id_map map;
    unsigned c, style, bank, index;
    int rc;
    cluster.u64 = cvmx_read_csr_node(0, CVMX_PKI_ICGX_CFG(0));
    /* CN78XX on this board has four clusters (reset mask 0xf). Some SDK
     * generated comments say 0x3; preserve the actual four-cluster mask. */
    if (cluster.s.clusters != 15 || cluster.s.maxipe_use != 0x14)
        return -ENODEV;
    /* The parser CAM must be ours alone: an entry beyond the two written
     * below could steer frames to a style this driver never configured, and
     * two entries of one bank matching one frame is a CAM conflict, after
     * which the parser's state is unpredictable and the frame is lost. */
    for (c = 0; c < 4; c++)
        for (bank = 0; bank < 2; bank++)
            for (index = 0; index < 192; index++) {
                term.u64 = cvmx_read_csr_node(0, CVMX_PKI_CLX_PCAMX_TERMX(c, bank, index));
                if (term.s.valid && index >= 1)
                    return -EBUSY;
            }
    /* L2 parsing of the Ethernet frame behind the four-byte envelope: two CAM
     * entries, one per bank so no frame can match both, sort frames into four
     * styles, 8 untagged, 9 VLAN-tagged, 10 and 11 FE100 returns (a return's
     * DMAC-low word is ITMH bytes 2-3 then CMH bytes 0-1, always xx 00 10 00).
     * No ethertype entry names IP, so the parser never identifies an L3 or L4
     * layer and can raise no L3/L4 error opcode, which the receive path would
     * treat as a drop. A catch-all ethertype entry beside the VLAN entry did
     * exactly that conflict on the PA-5220 (2026-10-20): every tagged frame
     * matched both and vanished without a work entry. */
    ps.s.pm = 0;
    ps.s.style = 8;
    pc.s.fcs_pres = 1;
    skip.s.inst_skip = 4;
    sc.s.fcs_chk = 1;
    sc.s.fcs_strip = 1;
    cfg2.s.tag_inc = 1; /* PKI_STYLE()_TAG_SEL[TAG_IDX0] names the byte mask */
    alg.s.tt = 2; /* UNTAGGED: the group, not a tag lock, keeps a flow in order */
    buf.s.mb_size = FFN_DMA_BLOCK / 8;
    buf.s.first_skip = 5; /* WQE words 0..4, link word, then packet */
    buf.s.later_skip = 0;
    rc = trunk_steer_apply();
    if (rc)
        return rc;
    rc = trunk_qpg_write(9, 0, ilog2(trunk_groups));
    if (rc)
        return rc;
    TRUNK_CSR(CVMX_PKI_PKINDX_ICGSEL(8), 0);
    for (style = 8; style < 12; style++)
        TRUNK_CSR(CVMX_PKI_STYLEX_BUF(style), buf.u64);
    for (c = 0; c < 4; c++) {
        TRUNK_CSR(CVMX_PKI_CLX_PKINDX_STYLE(8, c), ps.u64);
        TRUNK_CSR(CVMX_PKI_CLX_PKINDX_CFG(8, c), pc.u64);
        TRUNK_CSR(CVMX_PKI_CLX_PKINDX_SKIP(8, c), skip.u64);
        for (style = 8; style < 12; style++) {
            sc.s.qpg_base = style < 10 ? 8 : 9;
            TRUNK_CSR(CVMX_PKI_CLX_STYLEX_CFG(style, c), sc.u64);
            TRUNK_CSR(CVMX_PKI_CLX_STYLEX_CFG2(style, c), cfg2.u64);
            TRUNK_CSR(CVMX_PKI_CLX_STYLEX_ALG(style, c), alg.u64);
        }
    }
    /* DMACL, bank 1: a return's fixed ITMH/CMH bytes send it to style 10. */
    rc = trunk_pcam_entry(1, 0, 0x0b, 8, 0xff, 0x00001000, 0x00ffffff, 2, 0);
    if (rc)
        return rc;
    /* ETHTYPE0, bank 0: 0x8100 adds one. It matches styles 8 and 10 (mask
     * 0xfd), so a return whose garbage ethertype happens to read 0x8100 lands
     * on style 11, configured like 10. */
    rc = trunk_pcam_entry(0, 0, 0x18, 8, 0xfd, 0x81000000, 0xffff0000, 1, 0);
    if (rc)
        return rc;
    cluster.s.pena = 1;
    TRUNK_CSR(CVMX_PKI_ICGX_CFG(0), cluster.u64);
    map.u64 = cvmx_read_csr_node(0, CVMX_BGXX_CMRX_RX_ID_MAP(0, 2));
    map.s.pknd = 8;
    TRUNK_CSR(CVMX_BGXX_CMRX_RX_ID_MAP(0, 2), map.u64);
    return 0;
}

static int trunk_pko_configure(void)
{
    union cvmx_pko_ptgfx_cfg fifo;
    union cvmx_pko_macx_cfg mac;
    union cvmx_pko_mci1_max_credx credit = {.u64 = 0};
    union cvmx_pko_pdm_cfg pdm;
    union cvmx_pko_ptf_iobp_cfg iobp;
    union cvmx_pko_l3_l2_sqx_channel channel = {.u64 = 0};
    union cvmx_pko_lutx lut = {.u64 = 0};
    union cvmx_bgxx_cmrx_config bgx;
    union cvmx_bgxx_smux_tx_append append;
    unsigned i;
    int rc;
    bgx.u64 = cvmx_read_csr_node(0, CVMX_BGXX_CMRX_CONFIG(0, 2));
    if (!bgx.s.enable || bgx.s.lmac_type != 4)
        return -ENOLINK;
    /* Four 2.5KiB PKO FIFOs for the sole 40G MAC. Don't steal an assigned
     * FIFO from an existing driver, even if that driver's netdev is down. */
    for (i = 0; i < 28; i++) {
        mac.u64 = cvmx_read_csr_node(0, CVMX_PKO_MACX_CFG(i));
        if (i != 12 && mac.s.fifo_num < 4)
            return -EBUSY;
    }
    fifo.u64 = cvmx_read_csr_node(0, CVMX_PKO_PTGFX_CFG(0));
    /* Reset pointers with the OLD size before assigning the new size. */
    fifo.s.reset = 1;
    cvmx_write_csr_node(0, CVMX_PKO_PTGFX_CFG(0), fifo.u64);
    cvmx_read_csr_node(0, CVMX_PKO_PTGFX_CFG(0));
    fifo.s.size = 4; /* 10KiB,0,0,0 */
    fifo.s.rate = 3; /* 50Gbps */
    fifo.s.reset = 0;
    TRUNK_CSR(CVMX_PKO_PTGFX_CFG(0), fifo.u64);
    mac.u64 = cvmx_read_csr_node(0, CVMX_PKO_MACX_CFG(12));
    mac.s.fifo_num = 31;
    mac.s.fcs_ena = 1;
    mac.s.min_pad_ena = 1;
    mac.s.fcs_sop_off = 0;
    mac.s.skid_max_cnt = 2; /* 4 BGX FIFO slices * 256 / 512 */
    TRUNK_CSR(CVMX_PKO_MACX_CFG(12), mac.u64);
    /* PKO generates FCS/padding. The link-only SDK bring-up leaves BGX
     * data FCS enabled too; enabling both appends two CRCs. The BCM strips
     * only the outer one, delivering four spurious bytes to the front port.
     * Match cvmx-helper-pko3's PKO-owned FCS branch exactly. */
    append.u64 = cvmx_read_csr_node(0, CVMX_BGXX_SMUX_TX_APPEND(0,2));
    append.s.fcs_d = 0;
    append.s.pad = 0;
    TRUNK_CSR(CVMX_BGXX_SMUX_TX_APPEND(0,2), append.u64);
    mac.s.fifo_num = 0;
    TRUNK_CSR(CVMX_PKO_MACX_CFG(12), mac.u64);
    credit.s.max_cred_lim = 4 * 8192 / 16;
    TRUNK_CSR(CVMX_PKO_MCI1_MAX_CREDX(12), credit.u64);
    iobp.u64 = cvmx_read_csr_node(0, CVMX_PKO_PTF_IOBP_CFG);
    iobp.s.max_read_size = 0x10;
    TRUNK_CSR(CVMX_PKO_PTF_IOBP_CFG, iobp.u64);
    pdm.u64 = cvmx_read_csr_node(0, CVMX_PKO_PDM_CFG);
    pdm.s.pko_pad_minlen = 60;
    TRUNK_CSR(CVMX_PKO_PDM_CFG, pdm.u64);
    /* Channel identity is separate from the MAC selected by L1. It must be
     * carried down the scheduling tree and mapped for BGX backpressure. */
    channel.s.cc_channel = 0xa00; /* PKI_CHAN_E BGX2/LMAC0 */
    lut.s.valid = 1; lut.s.pq_idx = 0; lut.s.queue_number = 0;
    TRUNK_CSR(CVMX_PKO_CHANNEL_LEVEL, 0); /* L2 */
    TRUNK_CSR(CVMX_PKO_L3_L2_SQX_CHANNEL(0), channel.u64);
    TRUNK_CSR(CVMX_PKO_LUTX(0x280), lut.u64); /* CN78XX compressed BGX2 */
    return 0;
}
#undef TRUNK_CSR

/* Completions of one queue; its lock is held. Returns the frames still in flight. */
static unsigned trunk_reap(struct ffn_txq *t)
{
    unsigned i, pending = 0;
    struct ffn_dma_pool *p = &dma_pools[0];
    for (i = 0; i < FFN_TX_SLOTS; i++) {
        int b = t->pending[i].index;
        u64 *done;
        if (b < 0)
            continue;
        done = p->buffer[b].cpu + FFN_TX_DONE_OFFSET;
        if (READ_ONCE(*done) == 0) {
            dma_rmb();
            cvmx_fpa3_free(p->buffer[b].cpu, __cvmx_fpa3_gaura(0, p->aura), 0);
            if (t->pending[i].skb) {
                dev_consume_skb_any(t->pending[i].skb);
                t->pending[i].skb = NULL;
            }
            t->pending[i].index = -1;
            t->done++;
        } else {
            pending++;
            if (time_after(jiffies, t->pending[i].submitted + 5 * HZ))
                WRITE_ONCE(trunk_error, -ETIMEDOUT);
        }
    }
    return pending;
}

/* One frame onto descriptor queue q. raw: the skb already carries the
 * envelope (ffnpkt0). Otherwise it is an Ethernet frame from a port netdev
 * and the 24-byte envelope to dest is built here, the frame padded to 60. */
static netdev_tx_t trunk_send(struct sk_buff *skb, struct net_device *dev, struct ffn_port *port,
                              unsigned q, u16 dest, u8 key, bool raw)
{
    struct ffn_txq *t = &trunk_txq[q];
    struct ffn_dma_pool *p = &dma_pools[0];
    unsigned slot, total, n = 0, pad = 0, payload = 0;
    bool gather = false;
    int b;
    void *ptr;
    u8 *buf, envelope[FFN_TX_ENVELOPE];
    u64 words[FFN_PKO_WORDS_MAX], ack;
    unsigned long flags;
    spin_lock_irqsave(&t->lock, flags);
    trunk_reap(t);
    if (!trunk_accepting || trunk_error || !t->open)
        goto drop;
    if (raw) {
        /* Only the commissioned Jericho ITMH + RAW_DSA format is admitted.
         * An Ethernet stack must not accidentally inject ARP/IPv6 as a TM
         * command. The switch strips the eight-byte RAW_DSA area before
         * faceplate transmission, but its egress program reads it as a DSA
         * tag: byte 0 carries the tagged flag (a set flag makes it rebuild a
         * VLAN tag from the other bytes) and the upper four bytes carry
         * PCP/VID, so those five must stay zero. Bytes 1..3 carry the
         * aggregate owner's LAG load-balance key, which a field entry on the
         * trunk port copies into the switch's key. */
        total = skb->len;
        if (total < 26 || total > FFN_TRUNK_MAX || skb_copy_bits(skb, 0, envelope, sizeof(envelope)) ||
            !ffn_tx_envelope_valid(envelope, total))
            goto drop;
    } else {
        u8 macs[12];
        if (skb->len < FFN_ETH_HDR || skb_copy_bits(skb, 0, macs, sizeof(macs)))
            goto drop;
        total = ffn_tx_wire_len(skb->len);
        if (total > FFN_TRUNK_MAX)
            goto drop;
        payload = skb->len - 12;
        pad = total - FFN_TX_ENVELOPE - payload;
        ffn_tx_envelope(envelope, macs, dest, key);
        gather = READ_ONCE(tx_gather) && !skb_is_nonlinear(skb) &&
                 (cvmx_ptr_to_phys(skb->data + 12) + payload) <= FFN_PHYS_MASK;
    }
    for (slot = 0; slot < FFN_TX_SLOTS; slot++)
        if (t->pending[slot].index < 0)
            break;
    if (slot == FFN_TX_SLOTS) {
        netif_tx_stop_queue(netdev_get_tx_queue(dev, q));
        spin_unlock_irqrestore(&t->lock, flags);
        return NETDEV_TX_BUSY;
    }
    ptr = cvmx_fpa3_alloc(__cvmx_fpa3_gaura(0, p->aura));
    if (!ptr)
        goto drop;
    b = trunk_buffer(cvmx_ptr_to_phys(ptr), FFN_DMA_BLOCK);
    if (b < 0) {
        trunk_error = -EFAULT;
        trunk_bad_dma++;
        goto drop; /* retain unknown pointer */
    }
    buf = p->buffer[b].cpu;
    if (raw) {
        if (skb_copy_bits(skb, 0, buf, total))
            goto release;
    } else if (gather) {
        memcpy(buf, envelope, FFN_TX_ENVELOPE);
        memset(buf + FFN_TX_PAD_OFFSET, 0, pad);
    } else {
        memcpy(buf, envelope, FFN_TX_ENVELOPE);
        if (skb_copy_bits(skb, 12, buf + FFN_TX_ENVELOPE, payload))
            goto release;
        memset(buf + FFN_TX_ENVELOPE + payload, 0, pad);
    }
    WRITE_ONCE(*(u64 *)(buf + FFN_TX_DONE_OFFSET), ~0ULL);
    /* SEND_HDR DF=1; GATHER segments; SEND_MEM B64 SET0 completion. Completion
     * is separate from command acceptance and keeps every allocation
     * CPU-owned: PKO frees nothing, the reaper does. */
    words[n++] = ((u64)p->aura << 48) | (1ULL << 40) | total;
    if (gather) {
        words[n++] = ((u64)FFN_TX_ENVELOPE << 48) | (1ULL << 45) | p->buffer[b].dma;
        words[n++] = ((u64)payload << 48) | (1ULL << 45) | cvmx_ptr_to_phys(skb->data + 12);
        if (pad)
            words[n++] = ((u64)pad << 48) | (1ULL << 45) | (p->buffer[b].dma + FFN_TX_PAD_OFFSET);
    } else {
        words[n++] = ((u64)total << 48) | (1ULL << 45) | p->buffer[b].dma;
    }
    words[n++] = (0xcULL << 44) | (p->buffer[b].dma + FFN_TX_DONE_OFFSET);
    dma_wmb();
    ack = trunk_pko_command(0, words, n, q);
    trunk_last_ack = ack;
    /* Once a command has been issued, retain the buffer (and a gathered
     * skb) even on error. Some error statuses describe partial execution.
     * Only completion frees them. */
    t->pending[slot].index = b;
    t->pending[slot].skb = gather ? skb : NULL;
    t->pending[slot].submitted = jiffies;
    if (!ffn_pko_ack_ok(ack, 0)) {
        trunk_error = -EIO;
        t->drop++;
        port->tx_drop[q]++;
        if (!gather)
            dev_kfree_skb_any(skb);
        spin_unlock_irqrestore(&t->lock, flags);
        return NETDEV_TX_OK;
    }
    t->tx++;
    t->tx_bytes += total;
    port->tx[q]++;
    port->tx_bytes[q] += total;
    if (!gather)
        dev_consume_skb_any(skb);
    spin_unlock_irqrestore(&t->lock, flags);
    return NETDEV_TX_OK;
release:
    cvmx_fpa3_free(buf, __cvmx_fpa3_gaura(0, p->aura), 0);
drop:
    t->drop++;
    port->tx_drop[q]++;
    dev_kfree_skb_any(skb);
    spin_unlock_irqrestore(&t->lock, flags);
    return NETDEV_TX_OK;
}

static unsigned trunk_queue_of(struct sk_buff *skb)
{
    unsigned q = skb_get_queue_mapping(skb);
    return q < pko_queues ? q : 0;
}

static netdev_tx_t trunk_xmit(struct sk_buff *skb, struct net_device *dev)
{
    return trunk_send(skb, dev, &trunk_self, trunk_queue_of(skb), 0, 0, true);
}

static netdev_tx_t port_xmit(struct sk_buff *skb, struct net_device *dev)
{
    struct ffn_port *port = port_of(dev);
    unsigned q = trunk_queue_of(skb);
    u16 dest = port->sysport;
    u8 key = 0;
    if (port->lag) {
        u32 hash = skb_get_hash(skb);
        if (READ_ONCE(port->hardware)) {
            dest = FFN_LAG_SYSPORT(port->tid);
            key = ffn_lag_key(hash);
        } else {
            int m = ffn_lag_member(hash, READ_ONCE(port->distribute));
            if (m < 0) {
                unsigned long flags;
                spin_lock_irqsave(&trunk_txq[q].lock, flags);
                port->tx_drop[q]++;
                spin_unlock_irqrestore(&trunk_txq[q].lock, flags);
                dev_kfree_skb_any(skb);
                return NETDEV_TX_OK;
            }
            dest = trunk_port[m].sysport;
        }
    }
    return trunk_send(skb, dev, port, q, dest, key, false);
}

/* ffnret0 receives only: the FE100's returns are never transmitted by the DP. */
static netdev_tx_t return_xmit(struct sk_buff *skb, struct net_device *dev)
{
    struct ffn_port *port = port_of(dev);
    unsigned q = trunk_queue_of(skb);
    unsigned long flags;
    spin_lock_irqsave(&trunk_txq[q].lock, flags);
    port->tx_drop[q]++;
    spin_unlock_irqrestore(&trunk_txq[q].lock, flags);
    dev_kfree_skb_any(skb);
    return NETDEV_TX_OK;
}

/* A received frame, validated and copied into skb: who gets it. ffnpkt0 keeps
 * everything unless `deliver ports`; a port, LAG or return netdev gets its
 * frames when port_netdevs is set, cloned when ffnpkt0 keeps them too.
 * The frame enters the stack from this group's thread, its receive queue
 * recorded, so the fanout and RPS keep a flow on one consumer. */
static void trunk_deliver_skb(struct sk_buff *skb, unsigned g, unsigned len)
{
    struct ffn_rx_header h;
    struct ffn_port *target = NULL;
    enum ffn_deliver mode = READ_ONCE(trunk_deliver);
    bool to_trunk = true;
    skb_record_rx_queue(skb, g);
    if (port_netdevs && mode != FFN_DELIVER_TRUNK && !ffn_rx_header_parse(skb->data, len, &h)) {
        if (h.src == READ_ONCE(trunk_return_port) || ffn_rx_is_return(skb->data, len)) {
            target = &trunk_return;
        } else if (h.src < FFN_SYSPORT_MAP) {
            target = READ_ONCE(trunk_rx_map[h.src]);
            if (target) {
                struct ffn_port *lag = READ_ONCE(target->member_of);
                if (lag && !ffn_rx_is_control(skb->data, len)) {
                    if (READ_ONCE(lag->collect) & BIT(target->number - 1)) {
                        target = lag;
                    } else {
                        lag->rx_drop[g]++;
                        target = NULL;
                        if (mode == FFN_DELIVER_PORTS) {
                            kfree_skb(skb);
                            return;
                        }
                    }
                }
            } else {
                trunk_unmapped_group[g]++;
            }
        } else {
            trunk_unmapped_group[g]++;
        }
        to_trunk = mode != FFN_DELIVER_PORTS || !target;
    }
    if (target) {
        struct sk_buff *own = skb;
        if (to_trunk) {
            own = skb_clone(skb, GFP_ATOMIC);
            if (!own) {
                target->rx_drop[g]++;
                goto trunk_only;
            }
        }
        if (!netif_running(target->dev)) {
            target->rx_down[g]++;
            kfree_skb(own);
        } else {
            own->dev = target->dev;
            if (target->raw) {
                skb_reset_mac_header(own);
                own->protocol = htons(ETH_P_802_2);
                own->pkt_type = PACKET_OTHERHOST;
            } else {
                __skb_pull(own, FFN_RX_ENVELOPE);
                own->protocol = eth_type_trans(own, target->dev);
            }
            target->rx[g]++;
            target->rx_bytes[g] += own->len;
            netif_rx(own);
        }
        if (!to_trunk)
            return;
    }
trunk_only:
    skb->dev = trunk;
    skb_reset_mac_header(skb);
    skb->protocol = htons(ETH_P_802_2);
    skb->pkt_type = PACKET_OTHERHOST; /* AF_PACKET only; no host IP parse */
    trunk_self.rx[g]++;
    trunk_self.rx_bytes[g] += len;
    netif_rx(skb);
}

static int trunk_receive(unsigned g)
{
    /* SSO GET_WORK: node 0, grouped, return group, group g, NOWAIT. */
    u64 result = cvmx_read_csr((2ULL << 62) | (1ULL << 48) |
                              ((u64)CVMX_OCT_DID_TAG_SWTAG << 40) |
                              (1ULL << 30) | (1ULL << 29) | ((u64)g << 4));
    struct ffn_dma_pool *p = &dma_pools[0];
    struct sk_buff *skb = NULL;
    struct ffn_wqe_view v;
    int wi, indices[64];
    unsigned n, i, j, len, copied = 0;
    u64 *wqe, link;
    if (result >> 63)
        return 0;
    wi = trunk_buffer(result & FFN_PHYS_MASK, 40);
    if (wi < 0)
        goto corrupt;
    dma_rmb();
    wqe = p->buffer[wi].cpu + ((result & FFN_PHYS_MASK) - p->buffer[wi].dma);
    /* The entry may come from the PKI or from a userspace dataplane's punt;
     * both must say the kernel aura, port kind 8 and a sane chain. */
    ffn_wqe_decode(wqe[0], wqe[1], wqe[2], &v);
    link = wqe[3];
    n = v.bufs;
    len = v.len;
    if (!ffn_wqe_admissible(&v, p->aura, ARRAY_SIZE(indices)))
        goto corrupt;
    /* The classifier's group must be the one this thread drains; anything
     * else is a programming fault worth counting, not a corrupt buffer. */
    if (v.grp != g)
        trunk_mismatch_group[g]++;
    /* A punt outside the contract (an ordered or atomic tag): the next
     * GET_WORK releases the tag, so it only costs ordering, counted here. */
    if (v.tt != FFN_WQE_TT_UNTAGGED)
        trunk_punt_tagged_group[g]++;
    if (len >= 14 && len <= FFN_TRUNK_MAX && !v.errlev && !v.opcode)
        skb = netdev_alloc_skb_ip_align(trunk, len);
    for (i = 0; i < n; i++) {
        u64 addr = link & FFN_PHYS_MASK, next = 0;
        unsigned size = min_t(unsigned, link >> 48, len - copied);
        int b = trunk_buffer(addr, size);
        if (b < 0 || !size || addr < p->buffer[b].dma + 8)
            goto corrupt_skb;
        for (j = 0; j < i; j++)
            if (indices[j] == b)
                goto corrupt_skb;
        indices[i] = b;
        if (i + 1 < n)
            memcpy(&next, p->buffer[b].cpu + (addr - p->buffer[b].dma) - 8, 8);
        if (skb)
            skb_put_data(skb, p->buffer[b].cpu + (addr - p->buffer[b].dma), size);
        copied += size;
        link = next;
    }
    if (copied != len)
        goto corrupt_skb;
    /* Validate the entire chain before freeing any of it. Shared WQE/data
     * buffer is returned once, including errored and oversized packets. */
    for (i = 0; i < n; i++)
        cvmx_fpa3_free(p->buffer[indices[i]].cpu, __cvmx_fpa3_gaura(0, p->aura), 0);
    for (i = 0; i < n && indices[i] != wi; i++)
        ;
    if (i == n)
        cvmx_fpa3_free(p->buffer[wi].cpu, __cvmx_fpa3_gaura(0, p->aura), 0);
    if (skb) {
        trunk_rx_group[g]++;
        trunk_rx_bytes_group[g] += len;
        trunk_deliver_skb(skb, g, len);
    } else {
        trunk_rx_drop_group[g]++;
    }
    return 1;
corrupt_skb:
    kfree_skb(skb);
corrupt:
    trunk_bad_dma++;
    WRITE_ONCE(trunk_error, -EFAULT);
    return -EFAULT; /* no speculative frees */
}

/* Wake transmit queue q of every netdev that stopped it. */
static void trunk_wake_queue(unsigned q)
{
    unsigned i;
    netif_tx_wake_queue(netdev_get_tx_queue(trunk, q));
    if (!port_netdevs)
        return;
    netif_tx_wake_queue(netdev_get_tx_queue(trunk_return.dev, q));
    for (i = 0; i < FFN_FACEPLATE_PORTS; i++)
        netif_tx_wake_queue(netdev_get_tx_queue(trunk_port[i].dev, q));
    for (i = 0; i < FFN_LAGS_MAX; i++)
        if (trunk_lag[i].dev)
            netif_tx_wake_queue(netdev_get_tx_queue(trunk_lag[i].dev, q));
}

static int trunk_poll(void *argument)
{
    unsigned g = (unsigned long)argument, idle_us = 0;
    while (!kthread_should_stop()) {
        unsigned n = 0, q;
        unsigned long flags;
        if (READ_ONCE(trunk_running) && !READ_ONCE(trunk_error))
            for (; n < 64 && trunk_receive(g) > 0; n++)
                ;
        if (!g) {
            /* Completions, the transmit queues and the fault path belong to
             * the first thread; the others only drain their groups. */
            for (q = 0; q < pko_queues; q++) {
                unsigned pending;
                spin_lock_irqsave(&trunk_txq[q].lock, flags);
                pending = trunk_reap(&trunk_txq[q]);
                if (trunk_accepting && !trunk_error && pending < FFN_TX_SLOTS)
                    trunk_wake_queue(q);
                spin_unlock_irqrestore(&trunk_txq[q].lock, flags);
            }
            if (READ_ONCE(trunk_error)) {
                union cvmx_pki_buf_ctl ctl;
                ctl.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
                ctl.s.pki_en = 0;
                cvmx_write_csr_node(0, CVMX_PKI_BUF_CTL, ctl.u64);
                netif_carrier_off(trunk);
                netif_tx_stop_all_queues(trunk);
            }
        }
        /* Anything received means more may be queued: go straight back.
         * Otherwise back off from 50 us towards 500 us while the group stays
         * empty, so idle threads cost little and the first packet of a burst
         * waits half a millisecond at most. */
        if (n) {
            idle_us = 0;
            cond_resched();
            continue;
        }
        idle_us = idle_us ? min(idle_us * 2, 500u) : 50;
        usleep_range(idle_us, idle_us + idle_us / 2);
    }
    return 0;
}

static int trunk_open(struct net_device *dev)
{
    union cvmx_pki_buf_ctl pki;
    union cvmx_bgxx_cmrx_config bgx;
    u64 ack;
    unsigned q;
    int rc;
    mutex_lock(&init_lock);
    rc = trunk_error ?: quiescent();
    if (rc)
        goto out;
    bgx.u64 = cvmx_read_csr_node(0, CVMX_BGXX_CMRX_CONFIG(0, 2));
    if (!bgx.s.enable || !bgx.s.data_pkt_rx_en || !bgx.s.data_pkt_tx_en ||
        bgx.s.lmac_type != 4) {
        rc = -ENOLINK;
        goto out; /* link commissioning has not run since boot; no writes */
    }
    if (!prepared || !dma_ready || !sso_prepared || !pko_queues_prepared || dma_error) {
        rc = -EAGAIN;
        goto out;
    }
    if (!trunk_configured) {
        trunk_stage = "pki-configure";
        rc = trunk_pki_configure();
        if (!rc) {
            trunk_stage = "pko-configure";
            rc = trunk_pko_configure();
        }
        if (rc)
            goto failed;
        trunk_configured = true;
    }
    trunk_stage = "tag-configure";
    rc = trunk_tag_configure();
    if (rc)
        goto failed;
    trunk_stage = "pko-enable";
    rc = csr_checked(CVMX_PKO_ENABLE, 1);
    if (rc)
        goto failed;
    trunk_stage = "dq-open";
    for (q = 0; q < pko_queues; q++) {
        ack = trunk_pko_command(1, NULL, 0, q);
        trunk_last_ack = ack;
        if (!ffn_pko_ack_ok(ack, 1)) {
            rc = -EIO;
            goto failed;
        }
        trunk_txq[q].open = true;
    }
    cvmx_write_csr_node(0, CVMX_SSO_AW_CFG,
                       cvmx_read_csr_node(0, CVMX_SSO_AW_CFG) | 1);
    WRITE_ONCE(trunk_running, true);
    pki.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
    pki.s.pki_en = 1;
    rc = csr_checked(CVMX_PKI_BUF_CTL, pki.u64);
    if (rc)
        goto failed;
    netif_carrier_on(dev);
    WRITE_ONCE(trunk_accepting, true);
    netif_tx_start_all_queues(dev);
    trunk_stage = "running";
    goto out;
failed:
    WRITE_ONCE(trunk_accepting, false);
    WRITE_ONCE(trunk_running, false);
    trunk_error = rc; /* pinned memory; reset required on partial setup */
out:
    mutex_unlock(&init_lock);
    return rc;
}

static int trunk_stop(struct net_device *dev)
{
    union cvmx_pki_buf_ctl pki;
    unsigned i, q;
    unsigned long flags;
    u64 ack;
    /* Keep the worker draining RX, but prohibit it from waking TX while
     * ndo_stop is closing the descriptor queues. Serialize with both xmit
     * and the worker's completion/wake section. */
    for (q = 0; q < pko_queues; q++) {
        spin_lock_irqsave(&trunk_txq[q].lock, flags);
        trunk_accepting = false;
        spin_unlock_irqrestore(&trunk_txq[q].lock, flags);
    }
    netif_tx_disable(dev);
    netif_carrier_off(dev);
    mutex_lock(&init_lock);
    pki.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
    pki.s.pki_en = 0;
    cvmx_write_csr_node(0, CVMX_PKI_BUF_CTL, pki.u64);
    /* Worker drains RX and DMA completions while ingress is disabled. */
    for (i = 0; i < 1000; i++) {
        unsigned pending = 0;
        for (q = 0; q < pko_queues; q++) {
            spin_lock_irqsave(&trunk_txq[q].lock, flags);
            pending += trunk_reap(&trunk_txq[q]);
            spin_unlock_irqrestore(&trunk_txq[q].lock, flags);
        }
        if (!pending) {
            unsigned g, queued = 0;
            for (g = 0; g < trunk_groups; g++)
                queued += cvmx_read_csr_node(0, CVMX_SSO_GRPX_AQ_CNT(g));
            if (!queued)
                break;
        }
        usleep_range(1000, 2000);
    }
    WRITE_ONCE(trunk_running, false);
    if (i == 1000) {
        trunk_error = -ETIMEDOUT;
    } else {
        for (q = 0; q < pko_queues; q++) {
            if (!trunk_txq[q].open)
                continue;
            ack = trunk_pko_command(2, NULL, 0, q);
            trunk_last_ack = ack;
            if (!ffn_pko_ack_ok(ack, 2))
                trunk_error = -EIO;
            else
                trunk_txq[q].open = false;
        }
        if (!trunk_error)
            cvmx_write_csr_node(0, CVMX_PKO_ENABLE, 0);
    }
    trunk_stage = trunk_error ? "fault" : "stopped";
    mutex_unlock(&init_lock);
    return 0;
}

static void port_stats(struct net_device *dev, struct rtnl_link_stats64 *s)
{
    struct ffn_port *port = port_of(dev);
    s->rx_packets = trunk_sum(port->rx, FFN_RX_GROUPS_MAX);
    s->rx_bytes = trunk_sum(port->rx_bytes, FFN_RX_GROUPS_MAX);
    s->rx_dropped = trunk_sum(port->rx_drop, FFN_RX_GROUPS_MAX) + trunk_sum(port->rx_down, FFN_RX_GROUPS_MAX);
    s->tx_packets = trunk_sum(port->tx, FFN_TXQ_MAX);
    s->tx_bytes = trunk_sum(port->tx_bytes, FFN_TXQ_MAX);
    s->tx_dropped = trunk_sum(port->tx_drop, FFN_TXQ_MAX);
    if (port == &trunk_self)
        s->rx_errors = READ_ONCE(trunk_bad_dma);
}

static u16 port_select_queue(struct net_device *dev, struct sk_buff *skb, struct net_device *sb_dev)
{
    return ffn_tx_queue_for(raw_smp_processor_id(), READ_ONCE(pko_queues));
}

static int port_open(struct net_device *dev)
{
    netif_tx_start_all_queues(dev);
    return 0;
}

static int port_stop(struct net_device *dev)
{
    netif_tx_stop_all_queues(dev);
    return 0;
}

static const struct net_device_ops trunk_ops = {
    .ndo_open = trunk_open, .ndo_stop = trunk_stop, .ndo_start_xmit = trunk_xmit,
    .ndo_select_queue = port_select_queue, .ndo_get_stats64 = port_stats,
};
static const struct net_device_ops port_ops = {
    .ndo_open = port_open, .ndo_stop = port_stop, .ndo_start_xmit = port_xmit,
    .ndo_select_queue = port_select_queue, .ndo_get_stats64 = port_stats,
    .ndo_set_mac_address = eth_mac_addr, .ndo_validate_addr = eth_validate_addr,
};
static const struct net_device_ops return_ops = {
    .ndo_open = port_open, .ndo_stop = port_stop, .ndo_start_xmit = return_xmit,
    .ndo_select_queue = port_select_queue, .ndo_get_stats64 = port_stats,
};

/* One netdev of the driver, registered down with carrier off; rtnl is held. */
static int port_register(struct ffn_port *port, const char *name, unsigned number, u16 sysport,
                         bool raw, bool lag, const struct net_device_ops *ops)
{
    struct net_device *dev = alloc_etherdev_mqs(sizeof(struct ffn_port *), FFN_TXQ_MAX, FFN_RX_GROUPS_MAX);
    int rc;
    if (!dev)
        return -ENOMEM;
    strscpy(dev->name, name, IFNAMSIZ);
    *(struct ffn_port **)netdev_priv(dev) = port;
    dev->netdev_ops = ops;
    if (raw) {
        dev->mtu = 2048; dev->min_mtu = 1550; dev->max_mtu = FFN_TRUNK_MAX - ETH_HLEN;
        dev->flags |= IFF_NOARP;
        dev->priv_flags |= IFF_NO_ADDRCONF; /* raw envelope, no host IPv6 DAD */
    } else {
        dev->mtu = ETH_DATA_LEN; dev->min_mtu = ETH_MIN_MTU;
        dev->max_mtu = FFN_TRUNK_MAX - FFN_TX_ITMH - FFN_TX_DSA - ETH_HLEN;
    }
    eth_hw_addr_random(dev);
    SET_NETDEV_DEV(dev, dma_device);
    dev->needs_free_netdev = true; /* the core frees it after an unregister */
    netif_set_real_num_tx_queues(dev, pko_queues);
    netif_set_real_num_rx_queues(dev, trunk_groups);
    netif_carrier_off(dev);
    rc = register_netdevice(dev);
    if (rc) {
        free_netdev(dev);
        return rc;
    }
    memset(port, 0, sizeof(*port));
    port->dev = dev;
    port->number = number;
    port->sysport = sysport;
    port->raw = raw;
    port->lag = lag;
    if (!raw && !lag && sysport < FFN_SYSPORT_MAP)
        WRITE_ONCE(trunk_rx_map[sysport], port);
    return 0;
}

static void port_unregister_all(void)
{
    unsigned i;
    for (i = 0; i < FFN_FACEPLATE_PORTS; i++)
        if (trunk_port[i].dev) {
            unregister_netdevice(trunk_port[i].dev);
            trunk_port[i].dev = NULL;
        }
    if (trunk_return.dev) {
        unregister_netdevice(trunk_return.dev);
        trunk_return.dev = NULL;
    }
    memset(trunk_rx_map, 0, sizeof(trunk_rx_map));
}

static int trunk_register(void)
{
    unsigned i, g, cpu, q;
    int rc;
    if (trunk)
        return 0;
    /* Pass1 PKI/PKO errata need a separate implementation. */
    if (OCTEON_IS_MODEL(OCTEON_CN78XX_PASS1_X) ||
        CONFIG_CAVIUM_OCTEON_CVMSEG_SIZE < 2)
        return -EOPNOTSUPP;
    if (!dma_ready || !sso_prepared || !prepared || !pko_queues_prepared || dma_error)
        return -EAGAIN;
    trunk_groups = ffn_pow2_in(rx_groups, FFN_RX_GROUPS_MAX) ? rx_groups : FFN_RX_GROUPS_MAX;
    for (q = 0; q < FFN_TXQ_MAX; q++) {
        spin_lock_init(&trunk_txq[q].lock);
        for (i = 0; i < FFN_TX_SLOTS; i++)
            trunk_txq[q].pending[i].index = -1;
    }
    rc = port_register(&trunk_self, "ffnpkt%d", 0, 0, true, false, &trunk_ops);
    if (rc)
        return rc;
    trunk = trunk_self.dev;
    if (port_netdevs) {
        char name[IFNAMSIZ];
        trunk_deliver = FFN_DELIVER_BOTH;
        for (i = 1; i <= FFN_FACEPLATE_PORTS; i++) {
            snprintf(name, sizeof(name), "ffnp%u", i);
            rc = port_register(&trunk_port[i - 1], name, i, ffn_faceplate_sysport(i), false, false, &port_ops);
            if (rc)
                goto fail_ports;
        }
        rc = port_register(&trunk_return, "ffnret0", 0, 0, true, false, &return_ops);
        if (rc)
            goto fail_ports;
    }
    /* SSO core state must remain on the same core between work requests: one
     * thread per group, each pinned, leaving the boot CPU alone when there are
     * enough others. */
    cpu = cpumask_first(cpu_online_mask);
    if (num_online_cpus() > trunk_groups)
        cpu = cpumask_next(cpu, cpu_online_mask);
    for (g = 0; g < trunk_groups; g++) {
        struct task_struct *worker = kthread_create(trunk_poll, (void *)(unsigned long)g,
                                                   "ffn-packet-rx%u", g);
        if (IS_ERR(worker)) {
            rc = PTR_ERR(worker);
            while (g--)
                kthread_stop(trunk_workers[g]);
            goto fail_ports;
        }
        kthread_bind(worker, cpu);
        trunk_workers[g] = worker;
        trunk_cpu_group[g] = cpu;
        cpu = cpumask_next(cpu, cpu_online_mask);
        if (cpu >= nr_cpu_ids)
            cpu = cpumask_first(cpu_online_mask);
    }
    for (g = 0; g < trunk_groups; g++)
        wake_up_process(trunk_workers[g]);
    trunk_stage = "registered";
    return 0;
fail_ports:
    port_unregister_all(); /* every netdev frees itself once rtnl is dropped */
    unregister_netdevice(trunk);
    trunk = NULL;
    trunk_self.dev = NULL;
    return rc;
}

/* ---- the control file: steering, delivery, ports and LAGs; rtnl and init_lock held ---- */

static int control_mac(struct net_device *dev, const u8 *mac)
{
    struct sockaddr_storage ss;
    memset(&ss, 0, sizeof(ss));
    ss.ss_family = dev->type;
    memcpy(ss.__data, mac, ETH_ALEN);
    return dev_set_mac_address(dev, &ss, NULL);
}

static int control_apply(const struct ffn_ctl *c)
{
    struct ffn_port *port, *lag;
    int rc = 0;
    switch (c->op) {
    case FFN_CTL_STEER_KERNEL:
        trunk_steer_app = false;
        return trunk_configured ? trunk_steer_apply() : 0;
    case FFN_CTL_STEER_APP:
        if (!trunk || !ffn_app_groups_valid(c->a, c->b, trunk_groups))
            return -EINVAL;
        trunk_steer_app = true;
        trunk_app_base = c->a;
        trunk_app_log2 = c->b;
        return trunk_configured ? trunk_steer_apply() : 0;
    case FFN_CTL_DELIVER:
        if (c->a != FFN_DELIVER_TRUNK && !port_netdevs)
            return -ENODEV;
        WRITE_ONCE(trunk_deliver, c->a);
        return 0;
    case FFN_CTL_RETURN:
        WRITE_ONCE(trunk_return_port, c->a);
        return 0;
    case FFN_CTL_PORT_SYSPORT:
    case FFN_CTL_PORT_CARRIER:
    case FFN_CTL_PORT_MAC:
        if (!port_netdevs || !trunk)
            return -ENODEV;
        port = &trunk_port[c->a - 1];
        if (c->op == FFN_CTL_PORT_CARRIER) {
            if (c->flag)
                netif_carrier_on(port->dev);
            else
                netif_carrier_off(port->dev);
            return 0;
        }
        if (c->op == FFN_CTL_PORT_MAC)
            return control_mac(port->dev, c->mac);
        if (c->b == FFN_PORT_TRUNK || c->b == trunk_return_port ||
            (c->b < FFN_SYSPORT_MAP && trunk_rx_map[c->b] && trunk_rx_map[c->b] != port))
            return -EEXIST;
        if (port->sysport < FFN_SYSPORT_MAP && trunk_rx_map[port->sysport] == port)
            WRITE_ONCE(trunk_rx_map[port->sysport], NULL);
        port->sysport = c->b;
        if (c->b < FFN_SYSPORT_MAP)
            WRITE_ONCE(trunk_rx_map[c->b], port);
        return 0;
    case FFN_CTL_LAG_ADD:
    case FFN_CTL_LAG_DEL:
    case FFN_CTL_LAG_MEMBER:
    case FFN_CTL_LAG_COLLECT:
    case FFN_CTL_LAG_DISTRIBUTE:
    case FFN_CTL_LAG_HARDWARE:
    case FFN_CTL_LAG_CARRIER:
    case FFN_CTL_LAG_MAC:
        break;
    case FFN_CTL_NONE:
        return -EINVAL;
    }
    if (!port_netdevs || !trunk)
        return -ENODEV;
    lag = &trunk_lag[c->a - 1];
    if (c->op == FFN_CTL_LAG_ADD) {
        unsigned i;
        for (i = 0; i < FFN_LAGS_MAX; i++)
            if (trunk_lag[i].dev && &trunk_lag[i] != lag && trunk_lag[i].tid == c->b)
                return -EEXIST;
        if (!lag->dev) {
            char name[IFNAMSIZ];
            snprintf(name, sizeof(name), "ffnae%u", c->a);
            rc = port_register(lag, name, c->a, FFN_LAG_SYSPORT(c->b), false, true, &port_ops);
            if (rc)
                return rc;
        }
        lag->tid = c->b;
        lag->sysport = FFN_LAG_SYSPORT(c->b);
        return 0;
    }
    if (!lag->dev)
        return -ENOENT;
    switch (c->op) {
    case FFN_CTL_LAG_DEL: {
        unsigned i;
        for (i = 0; i < FFN_FACEPLATE_PORTS; i++)
            if (trunk_port[i].member_of == lag)
                WRITE_ONCE(trunk_port[i].member_of, NULL);
        WRITE_ONCE(lag->collect, 0);
        WRITE_ONCE(lag->distribute, 0);
        lag->member_mask = 0;
        lag->tid = 0;
        WRITE_ONCE(lag->hardware, false);
        netif_carrier_off(lag->dev);
        return 0;
    }
    case FFN_CTL_LAG_MEMBER:
        port = &trunk_port[c->b - 1];
        if (c->flag) {
            if (port->member_of && port->member_of != lag)
                return -EBUSY;
            WRITE_ONCE(port->member_of, lag);
            lag->member_mask |= BIT(c->b - 1);
        } else {
            if (port->member_of == lag)
                WRITE_ONCE(port->member_of, NULL);
            lag->member_mask &= ~BIT(c->b - 1);
            WRITE_ONCE(lag->collect, lag->collect & ~BIT(c->b - 1));
            WRITE_ONCE(lag->distribute, lag->distribute & ~BIT(c->b - 1));
        }
        return 0;
    case FFN_CTL_LAG_COLLECT:
    case FFN_CTL_LAG_DISTRIBUTE: {
        u32 *mask = c->op == FFN_CTL_LAG_COLLECT ? &lag->collect : &lag->distribute;
        if (!(lag->member_mask & BIT(c->b - 1)))
            return -ENOENT;
        WRITE_ONCE(*mask, c->flag ? (*mask | BIT(c->b - 1)) : (*mask & ~BIT(c->b - 1)));
        return 0;
    }
    case FFN_CTL_LAG_HARDWARE:
        WRITE_ONCE(lag->hardware, !!c->flag);
        return 0;
    case FFN_CTL_LAG_CARRIER:
        if (c->flag)
            netif_carrier_on(lag->dev);
        else
            netif_carrier_off(lag->dev);
        return 0;
    case FFN_CTL_LAG_MAC:
        return control_mac(lag->dev, c->mac);
    default:
        return -EINVAL;
    }
}

static int control_line(const char *line)
{
    struct ffn_ctl c;
    int rc;
    trunk_ctl_commands++;
    if (ffn_ctl_parse(line, &c)) {
        trunk_ctl_rejected++;
        return -EINVAL;
    }
    rc = control_apply(&c);
    if (rc)
        trunk_ctl_rejected++;
    return rc;
}

/* ---- status ---- */

static const char *trunk_deliver_name(void)
{
    static const char *names[] = { "trunk", "ports", "both" };
    return names[READ_ONCE(trunk_deliver)];
}

static void trunk_status(struct seq_file *m)
{
    unsigned g, q;
    u64 tx = 0, done = 0, drop = 0;
    for (q = 0; q < FFN_TXQ_MAX; q++) {
        tx += READ_ONCE(trunk_txq[q].tx);
        done += READ_ONCE(trunk_txq[q].done);
        drop += READ_ONCE(trunk_txq[q].drop);
    }
    seq_printf(m, "\"trunk\":{\"interface\":\"%s\",\"running\":%s,"
        "\"error\":%d,\"stage\":\"%s\",\"dq_open\":%s,\"last_ack\":\"0x%016llx\","
        "\"rx\":%llu,\"tx_accepted\":%llu,\"tx_completed\":%llu,"
        "\"wire_tx\":%llu,\"wire_rx\":%llu,\"tx_rejected\":%llu,"
        "\"bad_dma\":%llu,\"physical_forwarding_verified\":false,"
        "\"rx_groups\":%u,\"tag_mask_msb\":%u,\"group_mismatch\":%llu,"
        "\"tx_queues\":%u,\"port_netdevs\":%u,\"deliver\":\"%s\",\"steer\":\"%s\","
        "\"app_groups\":[%u,%u],\"unmapped\":%llu,\"punt_tagged\":%llu,\"rx_per_group\":[",
        trunk ? trunk->name : "", trunk_running ? "true" : "false", trunk_error,
        trunk_stage, trunk_txq[0].open ? "true" : "false", trunk_last_ack,
        trunk_sum(trunk_rx_group, FFN_RX_GROUPS_MAX), tx, done,
        cvmx_read_csr_node(0, CVMX_BGXX_CMRX_TX_STAT5(0,2)),
        cvmx_read_csr_node(0, CVMX_BGXX_CMRX_RX_STAT0(0,2)), drop, trunk_bad_dma,
        trunk_groups, tag_mask_msb, trunk_sum(trunk_mismatch_group, FFN_RX_GROUPS_MAX),
        pko_queues, port_netdevs, trunk_deliver_name(), trunk_steer_app ? "app" : "kernel",
        trunk_steer_app ? trunk_app_base : 0, trunk_steer_app ? 1u << trunk_app_log2 : 0,
        trunk_sum(trunk_unmapped_group, FFN_RX_GROUPS_MAX),
        trunk_sum(trunk_punt_tagged_group, FFN_RX_GROUPS_MAX));
    for (g = 0; g < trunk_groups; g++)
        seq_printf(m, "%s%llu", g ? "," : "", READ_ONCE(trunk_rx_group[g]));
    seq_puts(m, "],\"rx_drop_per_group\":[");
    for (g = 0; g < trunk_groups; g++)
        seq_printf(m, "%s%llu", g ? "," : "", READ_ONCE(trunk_rx_drop_group[g]));
    seq_puts(m, "],\"rx_cpus\":[");
    for (g = 0; g < trunk_groups; g++)
        seq_printf(m, "%s%u", g ? "," : "", trunk_cpu_group[g]);
    seq_puts(m, "],\"sso_backlog\":[");
    for (g = 0; g < trunk_groups; g++)
        seq_printf(m, "%s%llu", g ? "," : "",
                   (unsigned long long)cvmx_read_csr_node(0, CVMX_SSO_GRPX_AQ_CNT(g)));
    seq_puts(m, "]},");
}

static void port_row(struct seq_file *m, const struct ffn_port *port, const char *kind, bool first)
{
    unsigned i;
    seq_printf(m, "%s{\"name\":\"%s\",\"kind\":\"%s\",\"number\":%u,\"sysport\":%u,"
        "\"up\":%s,\"carrier\":%s,\"rx\":%llu,\"rx_bytes\":%llu,\"rx_drop\":%llu,\"rx_down\":%llu,"
        "\"tx\":%llu,\"tx_bytes\":%llu,\"tx_drop\":%llu",
        first ? "" : ",", port->dev->name, kind, port->number, port->sysport,
        netif_running(port->dev) ? "true" : "false", netif_carrier_ok(port->dev) ? "true" : "false",
        trunk_sum(port->rx, FFN_RX_GROUPS_MAX), trunk_sum(port->rx_bytes, FFN_RX_GROUPS_MAX),
        trunk_sum(port->rx_drop, FFN_RX_GROUPS_MAX), trunk_sum(port->rx_down, FFN_RX_GROUPS_MAX),
        trunk_sum(port->tx, FFN_TXQ_MAX), trunk_sum(port->tx_bytes, FFN_TXQ_MAX),
        trunk_sum(port->tx_drop, FFN_TXQ_MAX));
    if (port->member_of)
        seq_printf(m, ",\"member_of\":\"%s\"", port->member_of->dev->name);
    if (port->lag) {
        seq_printf(m, ",\"tid\":%u,\"hardware\":%s,\"members\":[", port->tid,
                   port->hardware ? "true" : "false");
        for (i = 0; i < FFN_FACEPLATE_PORTS; i++)
            if (port->member_mask & BIT(i))
                seq_printf(m, "{\"port\":%u,\"collect\":%s,\"distribute\":%s},", i + 1,
                           port->collect & BIT(i) ? "true" : "false",
                           port->distribute & BIT(i) ? "true" : "false");
        seq_puts(m, "{}]");
    }
    seq_puts(m, "}");
}

/* The `ports` file: every netdev of the driver with its counters, the
 * descriptor queues and the control state. Separate from `status`, whose
 * readers cap it at 4096 bytes. */
static void ports_status(struct seq_file *m)
{
    unsigned i, q;
    seq_printf(m, "{\"schema\":1,\"port_netdevs\":%u,\"deliver\":\"%s\",\"return_port\":%u,"
        "\"steer\":\"%s\",\"commands\":%llu,\"rejected\":%llu,\"txq\":[",
        port_netdevs, trunk_deliver_name(), trunk_return_port, trunk_steer_app ? "app" : "kernel",
        trunk_ctl_commands, trunk_ctl_rejected);
    for (q = 0; q < pko_queues; q++)
        seq_printf(m, "%s{\"dq\":%u,\"open\":%s,\"tx\":%llu,\"bytes\":%llu,\"done\":%llu,\"drop\":%llu}",
                   q ? "," : "", q, trunk_txq[q].open ? "true" : "false", READ_ONCE(trunk_txq[q].tx),
                   READ_ONCE(trunk_txq[q].tx_bytes), READ_ONCE(trunk_txq[q].done), READ_ONCE(trunk_txq[q].drop));
    seq_puts(m, "],\"netdevs\":[");
    if (trunk)
        port_row(m, &trunk_self, "trunk", true);
    if (trunk && port_netdevs) {
        port_row(m, &trunk_return, "return", false);
        for (i = 0; i < FFN_FACEPLATE_PORTS; i++)
            port_row(m, &trunk_port[i], "port", false);
        for (i = 0; i < FFN_LAGS_MAX; i++)
            if (trunk_lag[i].dev)
                port_row(m, &trunk_lag[i], "lag", false);
    }
    seq_puts(m, "]}\n");
}
