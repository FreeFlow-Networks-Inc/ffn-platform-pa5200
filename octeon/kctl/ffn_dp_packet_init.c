// SPDX-License-Identifier: GPL-2.0-only
/* PA-5220 CN78XX staged packet initialization.
 * Explicit commands prepare microcode, Linux DMA pools and queue memory.
 * The operator-supplied kernel tree carries Cavium's BSD-licensed microcode.
 */
#define CVMX_ENABLE_CSR_ADDRESS_CHECKING 0
#include <linux/module.h>
#include <linux/debugfs.h>
#include <linux/seq_file.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <asm/octeon/octeon.h>
#include <asm/octeon/cvmx-pki-defs.h>
#include <asm/octeon/cvmx-pko-defs.h>
/* Upstream POW still declares the old SSO threshold union. Keep that ABI
 * separate from the SDK's CN78XX register definitions in this module. */
#define cvmx_sso_wq_int_thrx ffn_cn78xx_sso_wq_int_thrx
#include <asm/octeon/cvmx-sso-defs.h>
#undef cvmx_sso_wq_int_thrx
#include <asm/octeon/cvmx-fpa-defs.h>
#define cvmx_pki_cluster_code_length ffn_pki_code_length
#define cvmx_pki_cluster_code_default ffn_pki_code
#include <asm/octeon/cvmx-pki-cluster.h>

static DEFINE_MUTEX(init_lock);
static struct dentry *directory;
static bool prepared;

static int quiescent(void)
{
    union cvmx_pki_sft_rst reset;
    union cvmx_pki_buf_ctl pki;
    union cvmx_pko_enable pko;
    reset.u64 = cvmx_read_csr_node(0, CVMX_PKI_SFT_RST);
    /* While BUSY, the hardware permits only reads of this register. */
    if (reset.s.busy || reset.s.active)
        return -EBUSY;
    pki.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
    pko.u64 = cvmx_read_csr_node(0, CVMX_PKO_ENABLE);
    return (pki.s.pki_en || pko.s.enable) ? -EBUSY : 0;
}

#include "ffn_dp_dma.h"
#include "ffn_dp_trunk.h"

static int status_show(struct seq_file *m, void *unused)
{
    union cvmx_pki_sft_rst reset;
    union cvmx_pki_buf_ctl pki;
    union cvmx_pko_enable pko;
    union cvmx_fpa_poolx_cfg pool;
    unsigned pools = 0, i;
    u64 sso;
    mutex_lock(&init_lock);
    reset.u64 = cvmx_read_csr_node(0, CVMX_PKI_SFT_RST);
    if (reset.s.busy) {
        seq_puts(m, "{\"schema\":1,\"pki_reset_busy\":true,\"ready\":false}\n");
        goto out;
    }
    pki.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
    pko.u64 = cvmx_read_csr_node(0, CVMX_PKO_ENABLE);
    sso = cvmx_read_csr_node(0, CVMX_SSO_AW_CFG);
    for (i = 0; i < 64; i++) {
        pool.u64 = cvmx_read_csr_node(0, CVMX_FPA_POOLX_CFG(i));
        pools += !!pool.s.ena;
    }
    seq_puts(m, "{");
    dma_status(m);
    trunk_status(m);
    seq_printf(m, "\"trunk_registered\":%s,", trunk ? "true" : "false");
    seq_printf(m, "\"schema\":1,\"pki_reset_busy\":false,"
        "\"pki_active\":%u,\"pki_enabled\":%u,\"pko_enabled\":%u,"
        "\"sso_aw_cfg\":\"0x%016llx\",\"enabled_fpa_pools\":%u,"
        "\"pki_microcode_prepared\":%s,\"microcode_words\":%zu,"
        "\"ready\":false}\n", (unsigned)reset.s.active,
        (unsigned)pki.s.pki_en, (unsigned)pko.s.enable,
        (unsigned long long)sso, pools, prepared ? "true" : "false",
        ARRAY_SIZE(ffn_pki_code));
out:
    mutex_unlock(&init_lock);
    return 0;
}
DEFINE_SHOW_ATTRIBUTE(status);

static ssize_t prepare_write(struct file *file, const char __user *buffer,
                             size_t length, loff_t *pos)
{
    char command[32];
    u64 *before;
    unsigned i;
    int rc;
    if (!capable(CAP_SYS_RAWIO))
        return -EPERM;
    if (!length || length >= sizeof(command))
        return -EINVAL;
    if (copy_from_user(command, buffer, length))
        return -EFAULT;
    command[length] = 0;
    strim(command);
    /* Match ndo_open/stop lock ordering and serialize raw debugfs writers,
     * including callers that bypass the Python commissioning lock. */
    if (!strcmp(command, "prepare-trunk")) {
        rtnl_lock();
        mutex_lock(&init_lock);
        rc = trunk_register();
        mutex_unlock(&init_lock);
        rtnl_unlock();
        return rc ? rc : length;
    }
    if (!strcmp(command,"prepare-dma") || !strcmp(command,"prepare-sso") ||
        !strcmp(command,"prepare-pko-memory") || !strcmp(command,"prepare-pko-queues")) {
        mutex_lock(&init_lock);
        if (!strcmp(command,"prepare-dma")) rc=prepare_dma();
        else if (!strcmp(command,"prepare-sso")) rc=prepare_sso();
        else if (!strcmp(command,"prepare-pko-memory")) rc=prepare_pko_memory();
        else rc=prepare_pko_queues();
        if (rc && dma_published && !dma_error) dma_error=rc;
        mutex_unlock(&init_lock);
        return rc ? rc : length;
    }
    if (strcmp(strim(command), "prepare-pki"))
        return -EINVAL;
    before = kmalloc_array(ARRAY_SIZE(ffn_pki_code), sizeof(*before), GFP_KERNEL);
    if (!before)
        return -ENOMEM;
    mutex_lock(&init_lock);
    rc = quiescent();
    if (rc)
        goto out;
    prepared = false;
    for (i = 0; i < ARRAY_SIZE(ffn_pki_code); i++)
        before[i] = cvmx_read_csr_node(0, CVMX_PKI_IMEMX(i));
    /* SDK cvmx_pki_setup_clusters: load every instruction before parse enable. */
    for (i = 0; i < ARRAY_SIZE(ffn_pki_code); i++)
        cvmx_write_csr_node(0, CVMX_PKI_IMEMX(i), ffn_pki_code[i]);
    for (i = 0; i < ARRAY_SIZE(ffn_pki_code); i++) {
        if (cvmx_read_csr_node(0, CVMX_PKI_IMEMX(i)) != ffn_pki_code[i]) {
            rc = -EIO;
            break;
        }
    }
    if (rc) {
        for (i = 0; i < ARRAY_SIZE(ffn_pki_code); i++)
            cvmx_write_csr_node(0, CVMX_PKI_IMEMX(i), before[i]);
        for (i = 0; i < ARRAY_SIZE(ffn_pki_code); i++)
            if (cvmx_read_csr_node(0, CVMX_PKI_IMEMX(i)) != before[i]) {
                pr_err("ffn_dp_packet_init: microcode rollback failed at %u\n", i);
                break;
            }
    } else {
        prepared = true;
    }
out:
    mutex_unlock(&init_lock);
    kfree(before);
    return rc ? rc : length;
}
static const struct file_operations prepare_fops = {
    .owner = THIS_MODULE, .write = prepare_write,
};

static int __init packet_init(void)
{
    if (!OCTEON_IS_MODEL(OCTEON_CN78XX))
        return -ENODEV;
    dma_device=root_device_register("ffn-cn78xx-packet-dma");
    if (IS_ERR(dma_device)) return PTR_ERR(dma_device);
    dma_device->dma_mask=&dma_device->coherent_dma_mask;
    if (dma_set_mask_and_coherent(dma_device,DMA_BIT_MASK(40))) {
        root_device_unregister(dma_device);
        return -ENODEV;
    }
    directory = debugfs_create_dir("ffn_dp_packet_init", NULL);
    if (IS_ERR(directory)) {
        root_device_unregister(dma_device);
        return PTR_ERR(directory);
    }
    debugfs_create_file("status", 0400, directory, NULL, &status_fops);
    debugfs_create_file("prepare", 0200, directory, NULL, &prepare_fops);
    return 0;
}
static void __exit packet_exit(void)
{
    debugfs_remove_recursive(directory);
    dma_release_unpublished();
    root_device_unregister(dma_device);
}
module_init(packet_init);
module_exit(packet_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("FFN CN78XX staged packet initialization");
