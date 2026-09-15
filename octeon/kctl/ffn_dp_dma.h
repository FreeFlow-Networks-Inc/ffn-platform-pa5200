/* SPDX-License-Identifier: GPL-2.0-only */
/* Included by the staged CN78XX initializer. Linux owns every DMA allocation.
 * SDK references: cvmx-fpa.c, cvmx-pow.c:cvmx_sso3_init and cvmx-helper-pko3.c.
 * Once a pointer is advertised, pin the module and allocations until reboot.
 * An incomplete initialization must never free memory still reachable by DMA.
 */
#include <linux/device.h>
#include <linux/dma-mapping.h>
#include <linux/delay.h>
#include <linux/bitmap.h>
#include <asm/octeon/ffn-cvmx-compat.h>
#include <asm/octeon/cvmx-address.h>
#include <asm/octeon/cvmx-fpa3.h>

#define FFN_DMA_BLOCK 4096
#define FFN_SSO_GROUPS 256
struct ffn_dma_buffer { void *cpu; dma_addr_t dma; };
struct ffn_dma_pool {
    unsigned id, aura, count;
    struct ffn_dma_buffer *buffer;
    struct ffn_dma_buffer stack;
    size_t stack_size;
    bool tested;
};
/* Keep pool zero disabled: unconfigured auras point at zero. */
static struct ffn_dma_pool dma_pools[] = {
    {.id=61, .aura=1021, .count=512}, /* packet/WQE */
    {.id=62, .aura=1022, .count=570}, /* 256*2+48+ceil(4096/416) XAQ */
    {.id=63, .aura=1023, .count=1024}, /* PKO3 DQ and jump buffers */
};
static struct device *dma_device;
static bool dma_published, dma_ready, sso_prepared, pko_memory_ready;
static bool pko_queues_prepared;
static int dma_error;

static int csr_checked(u64 reg, u64 value)
{
    u64 actual;
    cvmx_write_csr_node(0, reg, value);
    actual=cvmx_read_csr_node(0,reg);
    if (actual != value) {
        pr_err("ffn packet CSR %llx expected %llx got %llx\n",
            (unsigned long long)reg,(unsigned long long)value,(unsigned long long)actual);
        return -EIO;
    }
    return 0;
}

static void dma_release_unpublished(void)
{
    unsigned p, i;
    if (dma_published)
        return;
    for (p=0; p<ARRAY_SIZE(dma_pools); p++) {
        struct ffn_dma_pool *pool = &dma_pools[p];
        if (pool->buffer) {
            for (i=0; i<pool->count; i++)
                if (pool->buffer[i].cpu)
                    dma_free_coherent(dma_device, FFN_DMA_BLOCK,
                        pool->buffer[i].cpu, pool->buffer[i].dma);
            kfree(pool->buffer);
            pool->buffer = NULL;
        }
        if (pool->stack.cpu) {
            dma_free_coherent(dma_device, pool->stack_size,
                              pool->stack.cpu, pool->stack.dma);
            pool->stack.cpu = NULL;
        }
    }
}

static int dma_buffer_alloc(struct ffn_dma_buffer *b, size_t size)
{
    b->cpu = dma_alloc_coherent(dma_device, size, &b->dma, GFP_KERNEL);
    if (!b->cpu)
        return -ENOMEM;
    /* On-chip FPA consumes physical addresses, not PCI DMA translations.
     * Reject a bounce/IOMMU mapping rather than manufacturing a pointer. */
    if ((b->dma & 127) || (b->dma + size > (1ULL << 40)) ||
        cvmx_ptr_to_phys(b->cpu) != b->dma)
        return -ERANGE;
    memset(b->cpu, 0, size);
    return 0;
}

static int pool_publish(struct ffn_dma_pool *p)
{
    union cvmx_fpa_poolx_cfg cfg = {.u64=0};
    union cvmx_fpa_poolx_fpf_marks marks;
    union cvmx_fpa_aurax_int intr;
    dma_addr_t low = ~0ULL, high = 0;
    unsigned i;
    int rc;
#define SET_CSR(reg, value) do { rc=csr_checked((reg),(value)); if (rc) return rc; } while (0)
    for (i=0; i<p->count; i++) {
        low = min(low, p->buffer[i].dma);
        high = max(high, p->buffer[i].dma + FFN_DMA_BLOCK);
    }
    SET_CSR(CVMX_FPA_POOLX_STACK_BASE(p->id), p->stack.dma);
    SET_CSR(CVMX_FPA_POOLX_STACK_ADDR(p->id), p->stack.dma);
    SET_CSR(CVMX_FPA_POOLX_STACK_END(p->id), p->stack.dma+p->stack_size);
    marks.u64 = cvmx_read_csr_node(0, CVMX_FPA_POOLX_FPF_MARKS(p->id));
    marks.s.fpf_rd = 0x80; /* FPA-26117 / FPA-22443 */
    SET_CSR(CVMX_FPA_POOLX_FPF_MARKS(p->id), marks.u64);
    /* CN78XX encodes ADDR at bits 41:7; the older FPA1 layout used 32:0. */
    SET_CSR(CVMX_FPA_POOLX_START_ADDR(p->id), low);
    SET_CSR(CVMX_FPA_POOLX_END_ADDR(p->id), high);
    SET_CSR(CVMX_FPA_POOLX_THRESHOLD(p->id), p->count+1);
    cfg.cn78xx.buf_size = FFN_DMA_BLOCK >> 7;
    cfg.cn78xx.l_type = 2;
    cfg.cn78xx.nat_align = 1;
    SET_CSR(CVMX_FPA_POOLX_CFG(p->id), cfg.u64);
    cfg.cn78xx.ena = 1;
    SET_CSR(CVMX_FPA_POOLX_CFG(p->id), cfg.u64);
    SET_CSR(CVMX_FPA_AURAX_CNT(p->aura), p->count+32);
    SET_CSR(CVMX_FPA_AURAX_CNT_LIMIT(p->aura), p->count+32);
    SET_CSR(CVMX_FPA_AURAX_CNT_THRESHOLD(p->aura), p->count+33);
    for (i=0; i<100; i++) {
        intr.u64=0; intr.s.thresh=1;
        cvmx_write_csr_node(0, CVMX_FPA_AURAX_INT(p->aura), intr.u64);
        intr.u64=cvmx_read_csr_node(0, CVMX_FPA_AURAX_INT(p->aura));
        if (!intr.s.thresh) break;
        udelay(1);
    }
    if (i==100) return -ETIMEDOUT;
    /* Low byte is a hardware-maintained occupancy estimate, even while no
     * packet engines are enabled. Verify configuration bits only. Disable
     * optional QOS drops: CNT_LIMIT still enforces the hard allocation bound.
     * A zero DROP threshold with DROP_DIS=0 rejects every PKI allocation. */
    {
        u64 levels[] = {CVMX_FPA_AURAX_CNT_LEVELS(p->aura),
                        CVMX_FPA_AURAX_POOL_LEVELS(p->aura)};
        unsigned l;
        for (l = 0; l < ARRAY_SIZE(levels); l++) {
            cvmx_write_csr_node(0, levels[l], 1ULL << 40);
            if ((cvmx_read_csr_node(0, levels[l]) & ~255ULL) != (1ULL << 40))
                return -EIO;
        }
    }
    SET_CSR(CVMX_FPA_AURAX_CFG(p->aura), 0);
    SET_CSR(CVMX_FPA_AURAX_POOL(p->aura), p->id);
    dma_wmb();
    for (i=0; i<p->count; i++)
        cvmx_fpa3_free(p->buffer[i].cpu, __cvmx_fpa3_gaura(0,p->aura), 0);
    CVMX_SYNCWS;
    for (i=0; i<1000; i++) {
        if (cvmx_read_csr_node(0,CVMX_FPA_POOLX_AVAILABLE(p->id))==p->count &&
            cvmx_read_csr_node(0,CVMX_FPA_AURAX_CNT(p->aura))==32) return 0;
        udelay(10);
    }
    return -ETIMEDOUT;
}

static int pool_selftest(struct ffn_dma_pool *p)
{
    DECLARE_BITMAP(seen, 1024);
    unsigned i, j;
    int rc=0;
    void *ptr;
    bitmap_zero(seen, 1024);
    /* Keep all returned blocks out until uniqueness and exhaustion are proven. */
    for (i=0; i<p->count; i++) {
        ptr=cvmx_fpa3_alloc(__cvmx_fpa3_gaura(0,p->aura));
        if (!ptr) { rc=-ENOBUFS; break; }
        for (j=0; j<p->count; j++)
            if (p->buffer[j].dma == cvmx_ptr_to_phys(ptr)) break;
        if (j==p->count || test_and_set_bit(j,seen)) { rc=-EIO; break; }
    }
    if (!rc && cvmx_fpa3_alloc(__cvmx_fpa3_gaura(0,p->aura))) rc=-EOVERFLOW;
    /* Only return pointers whose ownership was verified. On corruption, retain
     * all allocations and refuse to hand the pool to any packet consumer. */
    for_each_set_bit(j, seen, p->count)
        cvmx_fpa3_free(p->buffer[j].cpu, __cvmx_fpa3_gaura(0,p->aura),0);
    CVMX_SYNCWS;
    for (i=0; i<1000; i++) {
        if (cvmx_read_csr_node(0,CVMX_FPA_AURAX_CNT(p->aura))==32 &&
            cvmx_read_csr_node(0,CVMX_FPA_POOLX_AVAILABLE(p->id))==p->count)
            break;
        udelay(10);
    }
    if (i==1000 && !rc) rc=-ETIMEDOUT;
    p->tested=!rc;
    return rc;
}

static int prepare_dma(void)
{
    unsigned p,i;
    int rc;
    if (dma_ready) return 0;
    if (dma_published) return -EUCLEAN;
    if (quiescent() || (cvmx_read_csr_node(0,CVMX_SSO_AW_CFG)&1) ||
        (cvmx_read_csr_node(0,CVMX_PKO_DPFI_ENA)&1)) return -EBUSY;
    /* Exclusive commissioning only; never adopt another allocator's pools. */
    for (i=0;i<64;i++)
        if (cvmx_read_csr_node(0,CVMX_FPA_POOLX_CFG(i))&1) return -EBUSY;
    for (p=0;p<ARRAY_SIZE(dma_pools);p++) {
        struct ffn_dma_pool *pool=&dma_pools[p];
        if (cvmx_read_csr_node(0,CVMX_FPA_AURAX_POOL(pool->aura))) return -EBUSY;
    }
    for (p=0;p<ARRAY_SIZE(dma_pools);p++) {
        struct ffn_dma_pool *pool=&dma_pools[p];
        pool->stack_size=ALIGN((pool->count*128)/29+128,128);
        pool->buffer=kcalloc(pool->count,sizeof(*pool->buffer),GFP_KERNEL);
        if (!pool->buffer) { rc=-ENOMEM; goto fail; }
        rc=dma_buffer_alloc(&pool->stack,pool->stack_size);
        if (rc) goto fail;
        for (i=0;i<pool->count;i++) {
            rc=dma_buffer_alloc(&pool->buffer[i],FFN_DMA_BLOCK);
            if (rc) goto fail;
        }
    }
    /* A reset is the recovery boundary after any hardware publication. */
    __module_get(THIS_MODULE);
    dma_published=true;
    cvmx_fpa3_config_red_params(0,0,3,1);
    for (p=0;p<ARRAY_SIZE(dma_pools);p++) {
        rc=pool_publish(&dma_pools[p]);
        if (!rc) rc=pool_selftest(&dma_pools[p]);
        if (rc) goto fail;
    }
    dma_ready=true;
    return 0;
fail:
    dma_error=rc;
    dma_release_unpublished();
    return rc;
}

static int sso_resources(void)
{
    union cvmx_sso_aw_we aw;
    union cvmx_sso_taq_cnt taq;
    union cvmx_sso_grpx_iaq_thr iaq_thr;
    union cvmx_sso_grpx_taq_thr taq_thr;
    union cvmx_sso_aw_add aw_add;
    union cvmx_sso_taq_add taq_add;
    unsigned ir,tr,im,tm,g;
    int rc;
    aw.u64=cvmx_read_csr_node(0,CVMX_SSO_AW_WE);
    taq.u64=cvmx_read_csr_node(0,CVMX_SSO_TAQ_CNT);
    ir=max_t(unsigned,2,aw.s.free_cnt/FFN_SSO_GROUPS/2);
    tr=max_t(unsigned,3,taq.s.free_cnt/FFN_SSO_GROUPS/2);
    im=min_t(unsigned,8191,ir<<7);
    tm=min_t(unsigned,2047,tr<<3);
    if (OCTEON_IS_MODEL(OCTEON_CN78XX_PASS1_X)) {
        tm=1264/FFN_SSO_GROUPS;
        tr=min(tr,tm);
    }
    if (aw.s.free_cnt<ir*FFN_SSO_GROUPS || taq.s.free_cnt<tr*FFN_SSO_GROUPS)
        return -ENOSPC;
    for(g=0;g<FFN_SSO_GROUPS;g++) {
        iaq_thr.u64=cvmx_read_csr_node(0,CVMX_SSO_GRPX_IAQ_THR(g));
        taq_thr.u64=cvmx_read_csr_node(0,CVMX_SSO_GRPX_TAQ_THR(g));
        aw_add.u64=taq_add.u64=0;
        aw_add.s.rsvd_free=ir-iaq_thr.s.rsvd_thr;
        taq_add.s.rsvd_free=tr-taq_thr.s.rsvd_thr;
        iaq_thr.s.rsvd_thr=ir; iaq_thr.s.max_thr=im;
        taq_thr.s.rsvd_thr=tr; taq_thr.s.max_thr=tm;
        SET_CSR(CVMX_SSO_GRPX_IAQ_THR(g),iaq_thr.u64);
        /* ADD registers are commands, not ordinary readback registers. */
        if(aw_add.s.rsvd_free) cvmx_write_csr_node(0,CVMX_SSO_AW_ADD,aw_add.u64);
        SET_CSR(CVMX_SSO_GRPX_TAQ_THR(g),taq_thr.u64);
        if(taq_add.s.rsvd_free) cvmx_write_csr_node(0,CVMX_SSO_TAQ_ADD,taq_add.u64);
    }
    aw.u64=cvmx_read_csr_node(0,CVMX_SSO_AW_WE);
    taq.u64=cvmx_read_csr_node(0,CVMX_SSO_TAQ_CNT);
    return aw.s.rsvd_free==ir*FFN_SSO_GROUPS &&
           taq.s.rsvd_free==tr*FFN_SSO_GROUPS ? 0 : -EIO;
}

static int prepare_sso(void)
{
    union cvmx_sso_xaqx_head_ptr head={.u64=0};
    union cvmx_sso_xaq_aura aura={.u64=0};
    struct ffn_dma_pool *p=&dma_pools[1];
    unsigned grp;
    int rc;
    void *ptr;
    if (sso_prepared) return 0;
    if (!dma_ready || dma_error) return -EAGAIN;
    if (quiescent() || (cvmx_read_csr_node(0,CVMX_SSO_AW_CFG)&1)) return -EBUSY;
    /* Refuse unknown queues before the first write. */
    for (grp=0;grp<FFN_SSO_GROUPS;grp++)
        if (cvmx_read_csr_node(0,CVMX_SSO_XAQX_HEAD_PTR(grp)) ||
            cvmx_read_csr_node(0,CVMX_SSO_XAQX_TAIL_PTR(grp)) ||
            cvmx_read_csr_node(0,CVMX_SSO_XAQX_HEAD_NEXT(grp)) ||
            cvmx_read_csr_node(0,CVMX_SSO_XAQX_TAIL_NEXT(grp))) return -EBUSY;
    for (grp=0;grp<FFN_SSO_GROUPS;grp++) {
        ptr=cvmx_fpa3_alloc(__cvmx_fpa3_gaura(0,p->aura));
        if (!ptr) return dma_error=-ENOBUFS;
        head.s.ptr=cvmx_ptr_to_phys(ptr)>>7;
        SET_CSR(CVMX_SSO_XAQX_HEAD_PTR(grp),head.u64);
        SET_CSR(CVMX_SSO_XAQX_HEAD_NEXT(grp),0);
        SET_CSR(CVMX_SSO_XAQX_TAIL_NEXT(grp),0);
        SET_CSR(CVMX_SSO_XAQX_TAIL_PTR(grp),head.u64);
    }
    aura.s.laura=p->aura; aura.s.node=0;
    SET_CSR(CVMX_SSO_XAQ_AURA,aura.u64);
    SET_CSR(CVMX_SSO_NW_TIM,0);
    rc=sso_resources();
    if(rc) return rc;
    /* Prepare only: RWEN remains clear until packet consumers are installed. */
    sso_prepared=true;
    return 0;
}

static int prepare_pko_queues(void)
{
    union cvmx_pko_l1_sqx_topology l1={.u64=0};
    union cvmx_pko_l1_sqx_shape shape={.u64=0};
    union cvmx_pko_l1_sqx_link link={.u64=0};
    union cvmx_pko_dqx_topology dq={.u64=0};
    union cvmx_pko_dqx_schedule sched={.u64=0};
    union cvmx_pko_dqx_wm_ctl wm={.u64=0};
    int rc;
    if(pko_queues_prepared) return 0;
    if(!pko_memory_ready || dma_error) return -EAGAIN;
    if(quiescent()) return -EBUSY;
    /* CN78XX BGX2/LMAC0 uses MAC 4+4*2=12. Dedicated single-child
     * L1->L2->L3->L4->L5->DQ path at index zero, as in SDK queue.c.
     * DQ remains CLOSED: no command DMA or packet output enabled here. */
    l1.s.link=12; l1.s.rr_prio=15;
    shape.s.link=12; link.s.link=12;
    SET_CSR(CVMX_PKO_L1_SQX_TOPOLOGY(0),l1.u64);
    SET_CSR(CVMX_PKO_L1_SQX_SHAPE(0),shape.u64);
    SET_CSR(CVMX_PKO_L1_SQX_LINK(0),link.u64);
#define CONFIG_LEVEL(n) do { \
    union cvmx_pko_l##n##_sqx_topology t={.u64=0}; \
    union cvmx_pko_l##n##_sqx_schedule s={.u64=0}; \
    t.s.parent=0; t.s.prio_anchor=0; t.s.rr_prio=15; s.s.rr_quantum=0x10; \
    SET_CSR(CVMX_PKO_L##n##_SQX_TOPOLOGY(0),t.u64); \
    SET_CSR(CVMX_PKO_L##n##_SQX_SCHEDULE(0),s.u64); \
} while(0)
    CONFIG_LEVEL(2); CONFIG_LEVEL(3); CONFIG_LEVEL(4); CONFIG_LEVEL(5);
#undef CONFIG_LEVEL
    sched.s.rr_quantum=0x10;
    wm.s.kind=1;
    SET_CSR(CVMX_PKO_DQX_TOPOLOGY(0),dq.u64);
    SET_CSR(CVMX_PKO_DQX_SCHEDULE(0),sched.u64);
    SET_CSR(CVMX_PKO_DQX_WM_CTL(0),wm.u64);
    pko_queues_prepared=true;
    return 0;
}

static int prepare_pko_memory(void)
{
    union cvmx_pko_dpfi_fpa_aura aura={.u64=0};
    union cvmx_pko_status status;
    unsigned i;
    int rc;
    if (pko_memory_ready) return 0;
    if (!dma_ready || dma_error) return -EAGAIN;
    if (quiescent() || (cvmx_read_csr_node(0,CVMX_PKO_DPFI_ENA)&1)) return -EBUSY;
    aura.s.laura=dma_pools[2].aura;
    SET_CSR(CVMX_PKO_DPFI_FLUSH,0);
    SET_CSR(CVMX_PKO_DPFI_FPA_AURA,aura.u64);
    SET_CSR(CVMX_PKO_DPFI_ENA,1);
    for (i=0;i<1000;i++) {
        status.u64=cvmx_read_csr_node(0,CVMX_PKO_STATUS);
        if (status.s.pko_rdy) { pko_memory_ready=true; return 0; }
        usleep_range(100,200);
    }
    /* PKO may still own prefetched buffers. Do not disable/free speculatively. */
    return dma_error=-ETIMEDOUT;
}
#undef SET_CSR

static void dma_status(struct seq_file *m)
{
    unsigned p;
    seq_printf(m,"\"dma_published\":%s,\"dma_ready\":%s,\"dma_error\":%d,"
        "\"sso_xaq_prepared\":%s,\"pko_memory_ready\":%s,\"pko_queues_prepared\":%s,\"dma_pools\":[",
        dma_published?"true":"false",dma_ready?"true":"false",dma_error,
        sso_prepared?"true":"false",pko_memory_ready?"true":"false",
        pko_queues_prepared?"true":"false");
    for(p=0;p<ARRAY_SIZE(dma_pools);p++) {
        struct ffn_dma_pool *pool=&dma_pools[p];
        seq_printf(m,"%s{\"pool\":%u,\"aura\":%u,\"block_size\":4096,"
            "\"capacity\":%u,\"tested\":%s,\"available\":%llu,\"aura_count\":%llu}",
            p?",":"",pool->id,pool->aura,pool->count,pool->tested?"true":"false",
            (unsigned long long)cvmx_read_csr_node(0,CVMX_FPA_POOLX_AVAILABLE(pool->id)),
            (unsigned long long)cvmx_read_csr_node(0,CVMX_FPA_AURAX_CNT(pool->aura)));
    }
    seq_puts(m,"],");
}
