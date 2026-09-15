// SPDX-License-Identifier: GPL-2.0-only
/* PA-5220 CN78XX BGX2 commissioning observation. No allocator calls or writes.
 * STATUS1 is latching-low: take two reads to report the current link.
 * This is an optional platform module, not a generic network driver.
 */
#define CVMX_ENABLE_CSR_ADDRESS_CHECKING 0
#include <linux/module.h>
#include <linux/debugfs.h>
#include <linux/seq_file.h>
#include <asm/octeon/octeon.h>
#include <asm/octeon/cvmx-bgxx-defs.h>
#include <asm/octeon/cvmx-pki-defs.h>

static struct dentry *directory;

static int status_show(struct seq_file *m, void *unused)
{
	union cvmx_bgxx_cmrx_config cfg;
	union cvmx_bgxx_spux_status1 link;
	union cvmx_bgxx_spux_br_status1 block;
	union cvmx_bgxx_cmrx_rx_id_map id;
	union cvmx_pki_buf_ctl pki;

	cfg.u64 = cvmx_read_csr_node(0, CVMX_BGXX_CMRX_CONFIG(0, 2));
	link.u64 = cvmx_read_csr_node(0, CVMX_BGXX_SPUX_STATUS1(0, 2));
	link.u64 = cvmx_read_csr_node(0, CVMX_BGXX_SPUX_STATUS1(0, 2));
	block.u64 = cvmx_read_csr_node(0, CVMX_BGXX_SPUX_BR_STATUS1(0, 2));
	id.u64 = cvmx_read_csr_node(0, CVMX_BGXX_CMRX_RX_ID_MAP(0, 2));
	pki.u64 = cvmx_read_csr_node(0, CVMX_PKI_BUF_CTL);
	seq_printf(m, "{\"schema\":1,\"bgx\":2,\"lmac\":0,"
		"\"enabled\":%u,\"rx_enabled\":%u,\"tx_enabled\":%u,"
		"\"lmac_type\":%u,\"link\":%u,\"block_lock\":%u,"
		"\"fault\":%u,\"pknd\":%u,\"pki_enabled\":%u}\n",
		(unsigned)cfg.s.enable, (unsigned)cfg.s.data_pkt_rx_en,
		(unsigned)cfg.s.data_pkt_tx_en, (unsigned)cfg.s.lmac_type,
		(unsigned)link.s.rcv_lnk, (unsigned)block.s.blk_lock,
		(unsigned)link.s.flt, (unsigned)id.s.pknd, (unsigned)pki.s.pki_en);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(status);

static int __init ffn_dp_link_init(void)
{
	if (!OCTEON_IS_MODEL(OCTEON_CN78XX))
		return -ENODEV;
	directory = debugfs_create_dir("ffn_dp_link", NULL);
	if (IS_ERR(directory))
		return PTR_ERR(directory);
	debugfs_create_file("status", 0400, directory, NULL, &status_fops);
	return 0;
}

static void __exit ffn_dp_link_exit(void)
{
	debugfs_remove_recursive(directory);
}
module_init(ffn_dp_link_init);
module_exit(ffn_dp_link_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("FFN PA-5220 DP internal link observation");
