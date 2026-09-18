// SPDX-License-Identifier: GPL-2.0-only
/* Read-only packet-engine commissioning snapshot. No arbitrary CSR access. */
#define CVMX_ENABLE_CSR_ADDRESS_CHECKING 0
#include <linux/module.h>
#include <linux/debugfs.h>
#include <linux/seq_file.h>
#include <asm/octeon/octeon.h>
#include <asm/octeon/cvmx-pki-defs.h>
#include <asm/octeon/cvmx-pko-defs.h>
#include <asm/octeon/cvmx-bgxx-defs.h>
static struct dentry *dir;
static int status_show(struct seq_file *m, void *unused)
{
    unsigned i;
    union cvmx_pki_sft_rst rst;
    rst.u64 = cvmx_read_csr_node(0, CVMX_PKI_SFT_RST);
    if (rst.s.busy) return -EBUSY;
#define SNAP(name, reg) seq_printf(m, name "=0x%016llx\n", cvmx_read_csr_node(0, reg))
    SNAP("pki_cluster", CVMX_PKI_ICGX_CFG(0));
    SNAP("pki_buffer", CVMX_PKI_BUF_CTL);
    SNAP("bgx_config", CVMX_BGXX_CMRX_CONFIG(0,2));
    SNAP("bgx_rx_map", CVMX_BGXX_CMRX_RX_ID_MAP(0,2));
    SNAP("bgx_tx_lmacs", CVMX_BGXX_CMR_TX_LMACS(2));
    SNAP("bgx_tx_append", CVMX_BGXX_SMUX_TX_APPEND(0,2));
    SNAP("bgx_rx_packets", CVMX_BGXX_CMRX_RX_STAT0(0,2));
    SNAP("bgx_rx_octets", CVMX_BGXX_CMRX_RX_STAT1(0,2));
    SNAP("bgx_rx_drops", CVMX_BGXX_CMRX_RX_STAT6(0,2));
    SNAP("bgx_tx_stat0", CVMX_BGXX_CMRX_TX_STAT0(0,2));
    SNAP("bgx_tx_stat1", CVMX_BGXX_CMRX_TX_STAT1(0,2));
    SNAP("bgx_tx_stat5", CVMX_BGXX_CMRX_TX_STAT5(0,2));
    SNAP("pko_fifo0", CVMX_PKO_PTGFX_CFG(0));
    SNAP("pko_status", CVMX_PKO_STATUS);
    SNAP("pko_dq_packets", CVMX_PKO_DQX_PACKETS(0));
    SNAP("pko_dq_bytes", CVMX_PKO_DQX_BYTES(0));
    SNAP("pko_l1_drops", CVMX_PKO_L1_SQX_DROPPED_PACKETS(0));
    SNAP("pko_peb_errors", CVMX_PKO_PEB_ERR_INT);
    SNAP("pko_ptf0", CVMX_PKO_PTFX_STATUS(0));
    SNAP("pko_l1_topology", CVMX_PKO_L1_SQX_TOPOLOGY(0));
    SNAP("pko_l1_shape", CVMX_PKO_L1_SQX_SHAPE(0));
    SNAP("pko_l1_link", CVMX_PKO_L1_SQX_LINK(0));
    SNAP("pko_channel_level", CVMX_PKO_CHANNEL_LEVEL);
    SNAP("pko_l2_channel", CVMX_PKO_L3_L2_SQX_CHANNEL(0));
    for (i=0; i<28; i++)
        seq_printf(m,"pko_mac%u=0x%016llx\n",i,cvmx_read_csr_node(0,CVMX_PKO_MACX_CFG(i)));
    return 0;
}
DEFINE_SHOW_ATTRIBUTE(status);
static int __init probe_init(void)
{
    if (!OCTEON_IS_MODEL(OCTEON_CN78XX)) return -ENODEV;
    dir=debugfs_create_dir("ffn_dp_packet_probe",NULL);
    if(IS_ERR(dir)) return PTR_ERR(dir);
    debugfs_create_file("status",0400,dir,NULL,&status_fops);
    return 0;
}
static void __exit probe_exit(void) { debugfs_remove_recursive(dir); }
module_init(probe_init);
module_exit(probe_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("FFN read-only CN78XX packet engine diagnostics");
