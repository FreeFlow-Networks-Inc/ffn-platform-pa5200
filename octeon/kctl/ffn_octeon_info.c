// SPDX-License-Identifier: GPL-2.0
/*
 * ffn_octeon_info.c -- publish /proc/octeon_info for the SDK's userspace runtime.
 *
 * WHY THIS EXISTS. The dataplane forwarder is an OCTEON userspace application:
 * ffn_dp_io_octeon3.c calls cvmx_user_app_init(), which is the SDK's Linux
 * user-mode entry point. That runtime finds the hardware by reading ONE file:
 *
 *     cvmx_sysinfo_linux_userspace_initialize()   [executive/cvmx-sysinfo.c:181]
 *         infile = fopen("/proc/octeon_info", "r");
 *         if (infile == NULL) { perror(...); exit(-1); }
 *
 * Note the exit(-1). There is no fallback and no degraded mode: without this
 * file the dataplane does not start, it dies in its first init call.
 *
 * The SDK's own 3.10 kernel publishes it. FFN runs 6.18, which does not, and
 * that single missing file was the whole gap between "the OCTEON III packet
 * backend compiles" and "traffic can flow through the DP".
 *
 * WHY IT IS THIS SMALL. Everything the parser wants is already in the kernel's
 * `octeon_bootinfo`, which arch/mips/cavium-octeon/setup.c fills in from the
 * boot descriptor u-boot passes, and which is EXPORT_SYMBOL'd. So this is a
 * formatter, not a source of truth -- it invents nothing, and if a value here is
 * wrong then the kernel's idea of the board is wrong too, which is a much more
 * useful thing to learn than a silent dataplane failure.
 *
 * THE FORMAT IS EXACT, and the parser is unforgiving in two specific ways:
 *
 *   1. It splits with strtok(buffer, " ") and compares the first token against
 *      "dram_size:" WITH the colon. Field name and colon are one token, so
 *      there must be no space before the colon and at least one after it.
 *   2. It reads lines with fgets into char[80]. A longer line is silently
 *      truncated and the remainder is parsed as though it were a new line.
 *      Nothing here comes close, but do not add a field that could.
 *
 * DELIBERATELY OMITTED: 32bit_shared_mem_base/size/wired. Those describe the
 * SDK kernel's 32-bit shared window, which this kernel has no equivalent of.
 * They are safe to leave out rather than invent -- cvmx-app-init-linux.c:246
 * guards the entire mapping behind `if (linux_mem32_min && linux_mem32_max)`,
 * so absent means zero means skipped. Emitting a plausible-looking made-up
 * range would instead have the runtime mmap a region nobody reserved.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>

#include <asm/octeon/octeon.h>
#include <asm/octeon/cvmx-bootinfo.h>

static int octeon_info_show(struct seq_file *m, void *v)
{
	const struct cvmx_bootinfo *bi = octeon_bootinfo;

	if (!bi) {
		/* Not an empty file: an empty one would parse cleanly as "every
		 * field zero" and send the dataplane off to use a NULL bootmem
		 * descriptor. Say so instead.
		 */
		seq_puts(m, "# octeon_bootinfo is NULL -- this kernel did not\n"
			    "# receive a boot descriptor. Nothing to publish.\n");
		return 0;
	}

	/* dram_size is already in MB here; the parser shifts it left by 20. */
	seq_printf(m, "dram_size: %u\n", bi->dram_size);
	seq_printf(m, "phy_mem_desc_addr: %u\n", bi->phy_mem_desc_addr);
	seq_printf(m, "eclock_hz: %u\n", bi->eclock_hz);
	/* The parser doubles this into dram_data_rate_hz, so pass it through. */
	seq_printf(m, "dclock_hz: %u\n", bi->dclock_hz);
	seq_printf(m, "board_type: %u\n", bi->board_type);
	seq_printf(m, "board_rev_major: %u\n", bi->board_rev_major);
	seq_printf(m, "board_rev_minor: %u\n", bi->board_rev_minor);
	seq_printf(m, "board_serial_number: %s\n",
		   bi->board_serial_number[0] ? bi->board_serial_number : "0");
	seq_printf(m, "mac_addr_base: %02x:%02x:%02x:%02x:%02x:%02x\n",
		   bi->mac_addr_base[0], bi->mac_addr_base[1],
		   bi->mac_addr_base[2], bi->mac_addr_base[3],
		   bi->mac_addr_base[4], bi->mac_addr_base[5]);
	seq_printf(m, "mac_addr_count: %u\n", bi->mac_addr_count);
	seq_printf(m, "fdt_addr: %llu\n", (unsigned long long)bi->fdt_addr);
	/* Read from the CPU rather than from bootinfo, which has no such field.
	 * cvmx_app_init_processor_id is what octeon_feature_init() keys the
	 * whole feature map off, so a wrong value here mis-describes the chip.
	 */
	seq_printf(m, "processor_id: %u\n", (unsigned int)read_c0_prid());

	return 0;
}

static int __init ffn_octeon_info_init(void)
{
	if (!proc_create_single("octeon_info", 0444, NULL, octeon_info_show))
		return -ENOMEM;

	pr_info("ffn_octeon_info: /proc/octeon_info published%s\n",
		octeon_bootinfo ? "" : " (WITHOUT a boot descriptor)");
	return 0;
}

static void __exit ffn_octeon_info_exit(void)
{
	remove_proc_entry("octeon_info", NULL);
}

module_init(ffn_octeon_info_init);
module_exit(ffn_octeon_info_exit);

MODULE_DESCRIPTION("FFN: /proc/octeon_info for the SDK userspace runtime");
MODULE_AUTHOR("FreeFlow Networks, Inc.");
MODULE_LICENSE("GPL");
