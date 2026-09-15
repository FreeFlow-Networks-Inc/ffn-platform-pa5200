/* SPDX-License-Identifier: GPL-2.0-only */
/* PA-5220 CN78XX raw internal trunk. Included after ffn_dp_dma.h.
 * Register layouts and command formats checked against the operator's SDK 5.1
 * cvmx-pki, cvmx-pko3 and octeon3-ethernet driver. No SDK allocator is used.
 * This is a commissioning driver: one PKND, one SSO group, one PKO DQ.
 * Linux AF_PACKET carries the FE100/BCM envelope without interpreting it.
 */
#include <linux/etherdevice.h>
#include <linux/kthread.h>
#include <linux/rtnetlink.h>
#include <asm/octeon/cvmx-scratch.h>
#include <asm/octeon/cvmx-bgxx-defs.h>

#define FFN_TRUNK_MAX 3584
#define FFN_TX_SLOTS 64
#define FFN_PHYS_MASK ((1ULL << 42) - 1)
static struct net_device *trunk;
static struct task_struct *trunk_worker;
static DEFINE_SPINLOCK(trunk_tx_lock);
static bool trunk_running, trunk_accepting, trunk_configured, trunk_dq_open;
static int trunk_error;
static const char *trunk_stage = "unprepared";
static u64 trunk_rx, trunk_rx_bytes, trunk_rx_drop, trunk_bad_dma;
static u64 trunk_tx, trunk_tx_bytes, trunk_tx_done, trunk_tx_drop;
static u64 trunk_last_ack;
static struct {
    int index;
    unsigned long submitted;
} trunk_pending[FFN_TX_SLOTS];

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
 * the real IOBDMA acknowledgement; never manufacture a successful result. */
static u64 trunk_pko_command(unsigned op, const u64 *words, unsigned count)
{
    unsigned long flags;
    u64 saved[4], ctl, result, command;
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
    command = ((u64)(ret >> 3) << 56) | (1ULL << 48) |
              (0x51ULL << 40) | ((u64)op << 32); /* node0, DQ0 */
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

static int trunk_pki_configure(void)
{
    union cvmx_pki_icgx_cfg cluster;
    union cvmx_pki_clx_pkindx_style ps = {.u64 = 0};
    union cvmx_pki_clx_pkindx_cfg pc = {.u64 = 0};
    union cvmx_pki_clx_stylex_cfg sc = {.u64 = 0};
    union cvmx_pki_clx_stylex_alg alg = {.u64 = 0};
    union cvmx_pki_stylex_buf buf = {.u64 = 0};
    union cvmx_pki_qpg_tblx qpg = {.u64 = 0};
    union cvmx_bgxx_cmrx_rx_id_map map;
    unsigned c;
    int rc;
#define TRUNK_CSR(r, v) do { rc = csr_checked((r), (v)); if (rc) return rc; } while (0)
    cluster.u64 = cvmx_read_csr_node(0, CVMX_PKI_ICGX_CFG(0));
    /* CN78XX on this board has four clusters (reset mask 0xf). Some SDK
     * generated comments say 0x3; preserve the actual four-cluster mask. */
    if (cluster.s.clusters != 15 || cluster.s.maxipe_use != 0x14)
        return -ENODEV;
    ps.s.pm = 0x7f; /* PARSE_NOTHING: preserve the proprietary header */
    ps.s.style = 8;
    pc.s.fcs_pres = 1;
    sc.s.qpg_base = 8;
    sc.s.fcs_chk = 1;
    sc.s.fcs_strip = 1;
    alg.s.tt = 2; /* UNTAGGED; no atomic/ordered tag locks to leak */
    buf.s.mb_size = FFN_DMA_BLOCK / 8;
    buf.s.first_skip = 5; /* WQE words 0..4, link word, then packet */
    buf.s.later_skip = 0;
    qpg.s.laura = dma_pools[0].aura;
    TRUNK_CSR(CVMX_PKI_PKINDX_ICGSEL(8), 0);
    TRUNK_CSR(CVMX_PKI_QPG_TBLX(8), qpg.u64);
    TRUNK_CSR(CVMX_PKI_QPG_TBLBX(8), 0);
    TRUNK_CSR(CVMX_PKI_STYLEX_BUF(8), buf.u64);
    for (c = 0; c < 4; c++) {
        TRUNK_CSR(CVMX_PKI_CLX_PKINDX_STYLE(8, c), ps.u64);
        TRUNK_CSR(CVMX_PKI_CLX_PKINDX_CFG(8, c), pc.u64);
        TRUNK_CSR(CVMX_PKI_CLX_PKINDX_SKIP(8, c), 0);
        TRUNK_CSR(CVMX_PKI_CLX_STYLEX_CFG(8, c), sc.u64);
        TRUNK_CSR(CVMX_PKI_CLX_STYLEX_CFG2(8, c), 0);
        TRUNK_CSR(CVMX_PKI_CLX_STYLEX_ALG(8, c), alg.u64);
    }
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

static unsigned trunk_reap(void)
{
    unsigned i, pending = 0;
    struct ffn_dma_pool *p = &dma_pools[0];
    for (i = 0; i < FFN_TX_SLOTS; i++) {
        int b = trunk_pending[i].index;
        u64 *done;
        if (b < 0)
            continue;
        done = p->buffer[b].cpu + FFN_DMA_BLOCK - 8;
        if (READ_ONCE(*done) == 0) {
            dma_rmb();
            cvmx_fpa3_free(p->buffer[b].cpu, __cvmx_fpa3_gaura(0, p->aura), 0);
            trunk_pending[i].index = -1;
            trunk_tx_done++;
        } else {
            pending++;
            if (time_after(jiffies, trunk_pending[i].submitted + 5 * HZ))
                WRITE_ONCE(trunk_error, -ETIMEDOUT);
        }
    }
    return pending;
}

static netdev_tx_t trunk_xmit(struct sk_buff *skb, struct net_device *dev)
{
    struct ffn_dma_pool *p = &dma_pools[0];
    unsigned slot;
    int b;
    void *ptr;
    u64 words[3], ack;
    u8 envelope[24];
    unsigned long flags;
    spin_lock_irqsave(&trunk_tx_lock, flags);
    trunk_reap();
    if (!trunk_accepting || trunk_error || skb->len < 14 || skb->len > FFN_TRUNK_MAX)
        goto drop;
    /* Only the commissioned Jericho ITMH + RAW_DSA format is admitted. An
     * Ethernet stack must not accidentally inject ARP/IPv6 as a TM command. */
    if (skb->len < 26 || skb_copy_bits(skb, 0, envelope, sizeof(envelope)) ||
        envelope[0] != 1 || envelope[3] || memchr_inv(envelope + 16, 0, 8))
        goto drop;
    for (slot = 0; slot < FFN_TX_SLOTS; slot++)
        if (trunk_pending[slot].index < 0)
            break;
    if (slot == FFN_TX_SLOTS) {
        netif_stop_queue(dev);
        spin_unlock_irqrestore(&trunk_tx_lock, flags);
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
    if (skb_copy_bits(skb, 0, p->buffer[b].cpu, skb->len)) {
        cvmx_fpa3_free(p->buffer[b].cpu, __cvmx_fpa3_gaura(0, p->aura), 0);
        goto drop;
    }
    WRITE_ONCE(*(u64 *)(p->buffer[b].cpu + FFN_DMA_BLOCK - 8), ~0ULL);
    /* SEND_HDR DF=1; GATHER; SEND_MEM B64 SET0 completion. Completion is
     * separate from command acceptance and keeps the allocation CPU-owned. */
    words[0] = ((u64)p->aura << 48) | (1ULL << 40) | skb->len;
    words[1] = ((u64)skb->len << 48) | (1ULL << 45) | p->buffer[b].dma;
    words[2] = (0xcULL << 44) | (p->buffer[b].dma + FFN_DMA_BLOCK - 8);
    dma_wmb();
    ack = trunk_pko_command(0, words, 3);
    trunk_last_ack = ack;
    /* Once a command has been issued, retain the buffer even on error. Some
     * error statuses describe partial execution. Only completion frees it. */
    trunk_pending[slot].index = b;
    trunk_pending[slot].submitted = jiffies;
    if ((ack >> 60) || ((ack >> 48) & 3)) {
        trunk_error = -EIO;
        goto drop;
    }
    trunk_tx++;
    trunk_tx_bytes += skb->len;
    dev_consume_skb_any(skb);
    spin_unlock_irqrestore(&trunk_tx_lock, flags);
    return NETDEV_TX_OK;
drop:
    trunk_tx_drop++;
    dev_kfree_skb_any(skb);
    spin_unlock_irqrestore(&trunk_tx_lock, flags);
    return NETDEV_TX_OK;
}

static int trunk_receive(void)
{
    /* SSO GET_WORK: node0, grouped, return group, group0, NOWAIT. */
    u64 result = cvmx_read_csr((2ULL << 62) | (1ULL << 48) |
                              ((u64)CVMX_OCT_DID_TAG_SWTAG << 40) |
                              (1ULL << 30) | (1ULL << 29));
    struct ffn_dma_pool *p = &dma_pools[0];
    struct sk_buff *skb = NULL;
    int wi, indices[64];
    unsigned n, i, j, len, copied = 0;
    u64 *wqe, word0, word1, word2, link;
    if (result >> 63)
        return 0;
    wi = trunk_buffer(result & FFN_PHYS_MASK, 40);
    if (wi < 0)
        goto corrupt;
    dma_rmb();
    wqe = p->buffer[wi].cpu + ((result & FFN_PHYS_MASK) - p->buffer[wi].dma);
    word0 = wqe[0]; word1 = wqe[1]; word2 = wqe[2]; link = wqe[3];
    n = (word0 >> 24) & 255;
    len = word1 >> 48;
    if (((word0 >> 48) & 4095) != p->aura || (word0 & 63) != 8 ||
        ((word1 >> 34) & 1023) != 0 || n == 0 || n > ARRAY_SIZE(indices))
        goto corrupt;
    if (len >= 14 && len <= FFN_TRUNK_MAX && !(word2 & 0x7ff))
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
        skb->dev = trunk;
        skb_reset_mac_header(skb);
        skb->protocol = htons(ETH_P_802_2);
        skb->pkt_type = PACKET_OTHERHOST; /* AF_PACKET only; no host IP parse */
        trunk_rx++; trunk_rx_bytes += len;
        netif_rx(skb);
    } else {
        trunk_rx_drop++;
    }
    return 1;
corrupt_skb:
    kfree_skb(skb);
corrupt:
    trunk_bad_dma++;
    WRITE_ONCE(trunk_error, -EFAULT);
    return -EFAULT; /* no speculative frees */
}

static int trunk_poll(void *unused)
{
    while (!kthread_should_stop()) {
        unsigned n, pending;
        unsigned long flags;
        if (READ_ONCE(trunk_running) && !READ_ONCE(trunk_error))
            for (n = 0; n < 64 && trunk_receive() > 0; n++)
                ;
        spin_lock_irqsave(&trunk_tx_lock, flags);
        pending = trunk_reap();
        if (trunk_accepting && !trunk_error && pending < FFN_TX_SLOTS)
            netif_wake_queue(trunk);
        spin_unlock_irqrestore(&trunk_tx_lock, flags);
        if (READ_ONCE(trunk_error)) {
            union cvmx_pki_buf_ctl ctl;
            ctl.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
            ctl.s.pki_en = 0;
            cvmx_write_csr_node(0, CVMX_PKI_BUF_CTL, ctl.u64);
            netif_carrier_off(trunk);
            netif_stop_queue(trunk);
        }
        usleep_range(500, 1000);
    }
    return 0;
}

static int trunk_open(struct net_device *dev)
{
    union cvmx_pki_buf_ctl pki;
    union cvmx_bgxx_cmrx_config bgx;
    u64 ack;
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
    trunk_stage = "pko-enable";
    rc = csr_checked(CVMX_PKO_ENABLE, 1);
    if (rc)
        goto failed;
    trunk_stage = "dq-open";
    ack = trunk_pko_command(1, NULL, 0);
    trunk_last_ack = ack;
    if ((ack >> 60) || ((ack >> 48) & 3) != 1) {
        rc = -EIO;
        goto failed;
    }
    trunk_dq_open = true;
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
    netif_start_queue(dev);
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
    unsigned i;
    unsigned long flags;
    u64 ack;
    /* Keep the worker draining RX, but prohibit it from waking TX while
     * ndo_stop is closing the descriptor queue. Serialize with both xmit
     * and the worker's completion/wake section. */
    spin_lock_irqsave(&trunk_tx_lock, flags);
    trunk_accepting = false;
    spin_unlock_irqrestore(&trunk_tx_lock, flags);
    netif_tx_disable(dev);
    netif_carrier_off(dev);
    mutex_lock(&init_lock);
    pki.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
    pki.s.pki_en = 0;
    cvmx_write_csr_node(0, CVMX_PKI_BUF_CTL, pki.u64);
    /* Worker drains RX and DMA completions while ingress is disabled. */
    for (i = 0; i < 1000; i++) {
        unsigned pending;
        unsigned long flags;
        spin_lock_irqsave(&trunk_tx_lock, flags);
        pending = trunk_reap();
        spin_unlock_irqrestore(&trunk_tx_lock, flags);
        if (!pending && !(cvmx_read_csr_node(0, CVMX_SSO_GRPX_AQ_CNT(0))))
            break;
        usleep_range(1000, 2000);
    }
    WRITE_ONCE(trunk_running, false);
    if (i == 1000) {
        trunk_error = -ETIMEDOUT;
    } else if (trunk_dq_open) {
        ack = trunk_pko_command(2, NULL, 0);
        trunk_last_ack = ack;
        if ((ack >> 60) || ((ack >> 48) & 3) != 2)
            trunk_error = -EIO;
        else {
            trunk_dq_open = false;
            cvmx_write_csr_node(0, CVMX_PKO_ENABLE, 0);
        }
    }
    trunk_stage = trunk_error ? "fault" : "stopped";
    mutex_unlock(&init_lock);
    return 0;
}

static void trunk_stats(struct net_device *dev, struct rtnl_link_stats64 *s)
{
    s->rx_packets = READ_ONCE(trunk_rx); s->rx_bytes = READ_ONCE(trunk_rx_bytes);
    s->rx_dropped = READ_ONCE(trunk_rx_drop); s->rx_errors = READ_ONCE(trunk_bad_dma);
    s->tx_packets = READ_ONCE(trunk_tx); s->tx_bytes = READ_ONCE(trunk_tx_bytes);
    s->tx_dropped = READ_ONCE(trunk_tx_drop);
}
static const struct net_device_ops trunk_ops = {
    .ndo_open = trunk_open, .ndo_stop = trunk_stop, .ndo_start_xmit = trunk_xmit,
    .ndo_get_stats64 = trunk_stats,
};

static int trunk_register(void)
{
    unsigned i;
    int rc;
    if (trunk)
        return 0;
    /* Pass1 PKI/PKO errata need a separate implementation. */
    if (OCTEON_IS_MODEL(OCTEON_CN78XX_PASS1_X) ||
        CONFIG_CAVIUM_OCTEON_CVMSEG_SIZE < 2)
        return -EOPNOTSUPP;
    if (!dma_ready || !sso_prepared || !prepared || !pko_queues_prepared || dma_error)
        return -EAGAIN;
    trunk = alloc_etherdev(0);
    if (!trunk)
        return -ENOMEM;
    strscpy(trunk->name, "ffnpkt%d", IFNAMSIZ);
    trunk->netdev_ops = &trunk_ops;
    trunk->mtu = 2048; trunk->min_mtu = 1550; trunk->max_mtu = FFN_TRUNK_MAX - ETH_HLEN;
    trunk->flags |= IFF_NOARP;
    trunk->priv_flags |= IFF_NO_ADDRCONF; /* raw envelope, no host IPv6 DAD */
    eth_hw_addr_random(trunk);
    SET_NETDEV_DEV(trunk, dma_device);
    netif_carrier_off(trunk);
    for (i = 0; i < FFN_TX_SLOTS; i++)
        trunk_pending[i].index = -1;
    rc = register_netdevice(trunk);
    if (rc)
        goto fail;
    trunk_worker = kthread_create(trunk_poll, NULL, "ffn-packet-rx");
    if (IS_ERR(trunk_worker)) {
        rc = PTR_ERR(trunk_worker);
        unregister_netdevice(trunk);
        goto fail;
    }
    /* SSO core state must remain on the same core between work requests. */
    kthread_bind(trunk_worker, cpumask_first(cpu_online_mask));
    wake_up_process(trunk_worker);
    trunk_stage = "registered";
    return 0;
fail:
    free_netdev(trunk);
    trunk = NULL;
    return rc;
}

static void trunk_status(struct seq_file *m)
{
    seq_printf(m, "\"trunk\":{\"interface\":\"%s\",\"running\":%s,"
        "\"error\":%d,\"stage\":\"%s\",\"dq_open\":%s,\"last_ack\":\"0x%016llx\","
        "\"rx\":%llu,\"tx_accepted\":%llu,\"tx_completed\":%llu,"
        "\"wire_tx\":%llu,\"wire_rx\":%llu,\"tx_rejected\":%llu,"
        "\"bad_dma\":%llu,\"physical_forwarding_verified\":false},",
        trunk ? trunk->name : "", trunk_running ? "true" : "false", trunk_error,
        trunk_stage, trunk_dq_open ? "true" : "false", trunk_last_ack,
        trunk_rx, trunk_tx, trunk_tx_done,
        cvmx_read_csr_node(0, CVMX_BGXX_CMRX_TX_STAT5(0,2)),
        cvmx_read_csr_node(0, CVMX_BGXX_CMRX_RX_STAT0(0,2)), trunk_tx_drop, trunk_bad_dma);
}
