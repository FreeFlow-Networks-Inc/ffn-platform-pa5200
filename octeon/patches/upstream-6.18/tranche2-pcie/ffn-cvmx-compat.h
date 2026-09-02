/*
 * FFN: compatibility shim for SDK cvmx sources built against upstream's cvmx.h.
 *
 * Upstream's asm/octeon/cvmx.h is a reduced version of the SDK's. The register
 * definition headers (*-defs.h) can simply be replaced with the SDK's, because
 * they carry no linkage and no ABI. cvmx.h cannot: it holds inline functions
 * and types shared with upstream's own compiled executive objects
 * (cvmx-helper.c, cvmx-bootmem.c and friends), so replacing it risks a silent
 * mismatch against code already in the tree. Shim the handful of missing
 * helpers instead.
 *
 * Include this AFTER asm/octeon/cvmx.h.
 */
#ifndef __FFN_CVMX_COMPAT_H__
#define __FFN_CVMX_COMPAT_H__

#include <linux/delay.h>
#include <linux/printk.h>

/*
 * Busy-wait helpers. The SDK spells these cvmx_wait()/cvmx_wait_usec();
 * upstream has neither.
 */
#ifndef cvmx_wait
static inline void cvmx_wait(uint64_t cycles)
{
	uint64_t done = read_c0_count() + (uint32_t)cycles;

	while ((int32_t)(read_c0_count() - (uint32_t)done) < 0)
		cpu_relax();
}
#define cvmx_wait cvmx_wait
#endif

#ifndef cvmx_wait_usec
static inline void cvmx_wait_usec(uint64_t usec)
{
	/*
	 * udelay() takes an unsigned long and callers here pass small
	 * constants, so split long waits rather than truncating.
	 */
	while (usec > 10000) {
		udelay(10000);
		usec -= 10000;
	}
	if (usec)
		udelay((unsigned long)usec);
}
#define cvmx_wait_usec cvmx_wait_usec
#endif

/* SDK diagnostic spellings. cvmx_dprintf already exists upstream. */
#ifndef cvmx_printf
#define cvmx_printf(...)	printk(__VA_ARGS__)
#endif
#ifndef cvmx_warn
#define cvmx_warn(...)		pr_warn(__VA_ARGS__)
#endif
#ifndef cvmx_warn_if
#define cvmx_warn_if(cond, ...)	do { if (cond) pr_warn(__VA_ARGS__); } while (0)
#endif

/*
 * Node-aware CSR poll. The SDK supports multi-socket OCTEON III, where a CSR
 * address is qualified by node; upstream has only the single-node form. This
 * platform is single-node -- the CN73XX control plane and the CN78XX data plane
 * are separate chips on separate PCIe segments, not sockets of one coherent
 * machine -- so drop the node and defer to upstream's macro. If multi-socket
 * support is ever needed, this is the place that has to grow, and it will fail
 * loudly rather than silently address node 0.
 */
#ifndef CVMX_WAIT_FOR_FIELD64_NODE
#define CVMX_WAIT_FOR_FIELD64_NODE(node, address, type, field, op, value, timeout_usec) \
	CVMX_WAIT_FOR_FIELD64(address, type, field, op, value, timeout_usec)
#endif

/*
 * CVMX_SHARED marks data shared between simple-executive cores. In a Linux
 * build there is one address space and the SDK defines it empty; upstream does
 * not define it at all, which is worse than it looks -- cvmx-qlm.c has
 *
 *     CVMX_SHARED qlm_jtag_uint32_t *__cvmx_qlm_jtag_xor_ref;
 *
 * so an undefined CVMX_SHARED makes that declaration unparseable and the
 * symbol then reads as undeclared everywhere it is used, several errors away
 * from the actual cause.
 */
#ifndef CVMX_SHARED
#define CVMX_SHARED
#endif

/*
 * The SDK spells bootmem descriptors with a _t alias. Upstream declares the
 * struct but not the alias.
 */
#ifndef __FFN_HAVE_NAMED_BLOCK_DESC_T
#define __FFN_HAVE_NAMED_BLOCK_DESC_T
typedef struct cvmx_bootmem_named_block_desc cvmx_bootmem_named_block_desc_t;
#endif

/*
 * CN73XX pass 1.2. Upstream octeon-model.h stops at PASS1_1; the SDK carries
 * this one and cvmx-qlm.c tests for it in an errata path. (This board reports
 * processor_id 0x000d9703, i.e. later than both.)
 */
#ifndef OCTEON_CN73XX_PASS1_2
#define OCTEON_CN73XX_PASS1_2   0x000d9702
#endif


/*
 * Coremask helpers. Upstream's cvmx-coremask.h is a reduced copy: it has
 * is_core_set/copy/set64/clear_core and the struct, but not the ones below.
 * They are needed by prom_init()'s ext_core_mask sanity check.
 *
 * The struct is layout-identical between the two trees -- both define
 * CVMX_MIPS_MAX_CORES 1024 over a 16 x u64 bitmap -- so scanning it here is
 * safe. The SDK spells its word size CVMX_COREMASK_HLDRSZ; upstream spells
 * the same 64 CVMX_COREMASK_ELTSZ, and upstream's cvmx.h already carries
 * CVMX_NODE_NO_SHIFT (7) and CVMX_NODE_BITS (2), so these are derived rather
 * than hardcoded. CVMX_MAX_USED_CORES_BMP works out to 512, exactly the
 * bound upstream's own "clear garbage upper bits" loop uses.
 */
#include <asm/octeon/cvmx-coremask.h>

#ifndef CVMX_COREMASK_MAX_CORES_PER_NODE
#define CVMX_COREMASK_MAX_CORES_PER_NODE	(1 << CVMX_NODE_NO_SHIFT)
#endif

#ifndef CVMX_MAX_USED_CORES_BMP
#define CVMX_MAX_USED_CORES_BMP \
	(1 << (CVMX_NODE_NO_SHIFT + CVMX_NODE_BITS))
#endif

#ifndef CVMX_COREMASK_USED_BMPSZ
#define CVMX_COREMASK_USED_BMPSZ \
	(CVMX_MAX_USED_CORES_BMP / CVMX_COREMASK_ELTSZ)
#endif

#ifndef cvmx_coremask_highest_bit
static inline int cvmx_coremask_highest_bit(uint64_t h)
{
	return (64 - __builtin_clzll(h) - 1);
}
#define cvmx_coremask_highest_bit cvmx_coremask_highest_bit
#endif

#ifndef cvmx_coremask_get_last_core
static inline int cvmx_coremask_get_last_core(const struct cvmx_coremask *pcm)
{
	int i;
	int found = -1;

	for (i = 0; i < CVMX_COREMASK_USED_BMPSZ; i++) {
		if (pcm->coremask_bitmap[i])
			found = i;
	}

	if (found == -1)
		return -1;

	return found * CVMX_COREMASK_ELTSZ +
	     cvmx_coremask_highest_bit(pcm->coremask_bitmap[found]);
}
#define cvmx_coremask_get_last_core cvmx_coremask_get_last_core
#endif

#ifndef cvmx_coremask_get_core_count
static inline int cvmx_coremask_get_core_count(const struct cvmx_coremask *pcm)
{
	int i;
	int count = 0;

	for (i = 0; i < CVMX_COREMASK_USED_BMPSZ; i++)
		count += __builtin_popcountll(pcm->coremask_bitmap[i]);

	return count;
}
#define cvmx_coremask_get_core_count cvmx_coremask_get_core_count
#endif
#endif /* __FFN_CVMX_COMPAT_H__ */
