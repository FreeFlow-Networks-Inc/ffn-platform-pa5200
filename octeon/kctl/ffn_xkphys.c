// SPDX-License-Identifier: GPL-2.0
/*
 * ffn_xkphys.c -- let user mode reach XKPHYS, so the dataplane can touch CSRs.
 *
 * WHY THE DATAPLANE NEEDS THIS. The forwarder is an OCTEON userspace
 * application, and in a CVMX_BUILD_FOR_LINUX_USER build every register access is
 * an inline load or store straight at an XKPHYS address:
 *
 *     CVMX_BUILD_READ64(uint64, "ld");     [cvmx-access-native.h]
 *     CVMX_BUILD_WRITE64(uint64, "sd");
 *
 * There is no ioctl and no /dev/mem in that path -- cvmx_read_csr() compiles to
 * `ld` from 0x8001... So without XKPHYS access the very first CSR read is a
 * SIGSEGV, and the dataplane dies before it can report anything useful.
 *
 * WHY A MODULE AND NOT THE SDK'S MECHANISM. The SDK asks for this per process:
 *
 *     sysmips(MIPS_CAVIUM_XKPHYS_WRITE, getpid(), 3, 0)   [cvmx-platform.h:167]
 *
 * Upstream's sysmips implements only MIPS_ATOMIC_SET and MIPS_FIXADE
 * (arch/mips/kernel/syscall.c), so that call returns -EINVAL here. Cavium's
 * kernel adds the command, two TIF_XKPHYS_* thread flags, and a
 * prepare_arch_switch hook that reloads the bits on every context switch --
 * an arch patch across thread_info.h, syscall.c and octeon-cpu.c.
 *
 * All of that machinery exists to make the permission PER PROCESS. This module
 * skips it and sets the bits machine-wide, which is a smaller and more honest
 * change than a partial port of a security mechanism.
 *
 * WHAT THAT COSTS, STATED PLAINLY. With these bits set, ANY user process on this
 * node can read and write ANY physical address and any device register, with no
 * further permission check. That is a real relaxation and it is not subtle.
 *
 * It is a defensible trade HERE and it should not be copied elsewhere without
 * re-making the argument:
 *   - The DP is a dedicated packet co-processor. It has no users, no shells for
 *     anyone but us, and runs one program.
 *   - Anything that can run code on it is already root -- and root already has
 *     /dev/mem, which grants the same access by a slower route.
 * On the control plane, which does have a userland, this reasoning does NOT
 * hold. Do not load it there.
 *
 * CvmMemCtl is PER CORE, so this has to run on all 40 of them -- hence
 * on_each_cpu(). A core that comes online later will not have the bits; the DP
 * brings all cores up at boot, so that case does not arise today, but it is the
 * thing to check first if the dataplane faults on one core and not others.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/smp.h>

#include <asm/octeon/octeon.h>
#include <asm/mipsregs.h>

static bool mem = true;
module_param(mem, bool, 0444);
MODULE_PARM_DESC(mem, "allow user XKPHYS access to DRAM (default on)");

static bool io = true;
module_param(io, bool, 0444);
MODULE_PARM_DESC(io, "allow user XKPHYS access to device CSRs (default on)");

static void xkphys_set(void *arg)
{
	union octeon_cvmemctl c;
	int on = *(int *)arg;

	c.u64 = read_c0_cvmmemctl();
	if (mem)
		c.s.xkmemenau = on;
	if (io)
		c.s.xkioenau = on;
	write_c0_cvmmemctl(c.u64);
}

/* Read one core's state back rather than trusting the write, because a write to
 * a reserved or read-only bit is silently dropped and would otherwise look like
 * success right up until the dataplane faults.
 */
static void xkphys_report(const char *what)
{
	union octeon_cvmemctl c;

	c.u64 = read_c0_cvmmemctl();
	pr_info("ffn_xkphys: %s -- cpu%d CvmMemCtl xkmemenau=%u xkioenau=%u\n",
		what, smp_processor_id(),
		(unsigned int)c.s.xkmemenau, (unsigned int)c.s.xkioenau);
}

static int __init ffn_xkphys_init(void)
{
	int on = 1;

	if (!mem && !io) {
		pr_err("ffn_xkphys: both mem and io are off; nothing to do\n");
		return -EINVAL;
	}

	on_each_cpu(xkphys_set, &on, 1);
	xkphys_report("enabled");
	pr_warn("ffn_xkphys: user-mode physical access is now open to EVERY "
		"process on this node. Dataplane nodes only.\n");
	return 0;
}

static void __exit ffn_xkphys_exit(void)
{
	int off = 0;

	on_each_cpu(xkphys_set, &off, 1);
	xkphys_report("disabled");
}

module_init(ffn_xkphys_init);
module_exit(ffn_xkphys_exit);

MODULE_DESCRIPTION("FFN: enable user-mode XKPHYS access for the OCTEON dataplane");
MODULE_AUTHOR("FreeFlow Networks, Inc.");
MODULE_LICENSE("GPL");
