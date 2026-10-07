/* SPDX-License-Identifier: GPL-2.0 */
/*
 * FFN NAC -- userspace ABI for the PA-5200 Nir-Aho-Corasick accelerators.
 *
 * Shared by the ffn_nac kernel module and any userspace that drives it.
 * Keep this file ABI-stable: bump FFN_NAC_ABI_VERSION on any layout change.
 */
#ifndef _FFN_NAC_ABI_H
#define _FFN_NAC_ABI_H

#include <linux/types.h>
#include <linux/ioctl.h>

#define FFN_NAC_ABI_VERSION	2

/*
 * The NAC family. Every one of these vendor modules carries the SAME modinfo
 * description -- "PAN Nir-Aho-Corsik (nac) Driver" -- and the PDT confirms they
 * are one design: usr/share/pdt/ce40.py and ca1.py are 1791 lines each and
 * differ by nothing but the name (sed s/ce40/ca1/ makes them byte-identical).
 * Same register map, same cip_partition_offset = 0x8800 + 0x1000*i, same
 * dfa_partition_offset = 0xc800 + 0x1000*i.
 *
 * 0xfeed is Palo Alto's private vendor id (the FE100 is feed:fe1c and feed:f101
 * on the same vendor, but it is a flow engine, not a NAC).
 */
#define FFN_NAC_VENDOR_ID	0xfeed

#define FFN_NAC_DEVICE_CHEETAH	0xa003
#define FFN_NAC_DEVICE_OCELOT	0xa009
#define FFN_NAC_DEVICE_CE40	0xa00d
#define FFN_NAC_DEVICE_CE10	0xa00e
#define FFN_NAC_DEVICE_CA1	0xa101
#define FFN_NAC_DEVICE_CA2	0xa102

/*
 * Capability bits, each one MEASURED from the vendor module's own strings and
 * symbols rather than assumed from the part number:
 *
 *   DDR_LAYOUTS  exports nac_ddr_layout_small/_medium/_large -- three
 *                automaton memory layouts. ce40, ca1, ca2 only.
 *   MEM_CHECK    has "<NAME> memory check failed" and "FPGA memory link is
 *                down - aborting!". ce40, ce10.
 *   RLD_CAL      has check_rld_status() plus "FPGA RLD CAL", "Get RLD CAL
 *                ERROR", "Get RLD Link issue, try to recover". ce40 ONLY.
 *   RLD_RETRY    has "RLDRAM Memory init retry/succeeded/incomplete" but no
 *                check_rld_status. ocelot only.
 *
 * This matters, and is not trivia: "Memory channels init incomplete" is
 * printk level 3 (KERN_ERR) in ca1.ko and level 4 (KERN_WARNING) in ce40.ko,
 * because ce40 has a recovery path and ca1 has NONE. On our board CA1's
 * memory-channel ready bit never rises and nothing in its driver would ever
 * fix that -- which is the single best reason to drive the CE40 instead.
 */
#define FFN_NAC_CAP_DDR_LAYOUTS	(1u << 0)
#define FFN_NAC_CAP_MEM_CHECK	(1u << 1)
#define FFN_NAC_CAP_RLD_CAL	(1u << 2)
#define FFN_NAC_CAP_RLD_RETRY	(1u << 3)

#define FFN_NAC_VARIANT_NAME_MAX	16

#define FFN_NAC_NUM_BARS	4

/*
 * The character device presents all BARs in ONE flat address space so that a
 * single lseek()/read()/write() can reach any of them.  Each BAR gets its own
 * 4 MiB-aligned slot, whatever its real length:
 *
 *   file offset            BAR   real length (observed)
 *   0x0000000..0x003FFFF   BAR0  256 KiB
 *   0x0400000..0x07FFFFF   BAR1    4 MiB
 *   0x0800000..0x0BFFFFF   BAR2    4 MiB
 *   0x0C00000..0x0FFFFFF   BAR3    4 MiB
 *
 * Accessing a hole (past a BAR's real length but inside its slot) returns
 * -ENXIO rather than reading off the end of the mapping.
 */
#define FFN_NAC_SLOT_SHIFT	22			/* 4 MiB per slot */
#define FFN_NAC_SLOT_SIZE	(1UL << FFN_NAC_SLOT_SHIFT)
#define FFN_NAC_SLOT_MASK	(FFN_NAC_SLOT_SIZE - 1)
#define FFN_NAC_WINDOW_SIZE	(FFN_NAC_NUM_BARS * FFN_NAC_SLOT_SIZE)

#define FFN_NAC_OFF_TO_BAR(off)	((__u32)((off) >> FFN_NAC_SLOT_SHIFT))
#define FFN_NAC_OFF_IN_BAR(off)	((__u32)((off) & FFN_NAC_SLOT_MASK))
#define FFN_NAC_BAR_OFF(bar)	((__u64)(bar) << FFN_NAC_SLOT_SHIFT)

struct ffn_nac_bar_info {
	__u64	phys;		/* bus address as assigned by the host bridge */
	__u64	len;		/* real BAR length in bytes; 0 = BAR absent   */
	__u64	slot_off;	/* this BAR's base in the chardev flat space  */
	__u32	flags;		/* raw IORESOURCE_* flags                     */
	__u32	__pad;
};

/*
 * NOTE ON LAYOUT: bar[] contains __u64 and therefore wants 8-byte alignment.
 * The scalar head below is padded to exactly 24 bytes so the compiler inserts
 * no implicit hole -- get this wrong and userspace unpacks at the wrong offset
 * and the ioctl size encoded in the request number stops matching.
 */
struct ffn_nac_info {
	__u32	abi_version;	/* 0  */
	__u16	vendor;		/* 4  */
	__u16	device;		/* 6  */
	__u8	revision;	/* 8  */
	__u8	num_bars;	/* 9  */
	__u8	write_enabled;	/* 10 mirrors the allow_write module parameter */
	__u8	__pad0;		/* 11 */
	__u32	pci_command;	/* 12 COMMAND register as it reads now         */
	__u32	caps;		/* 16 FFN_NAC_CAP_* for the bound variant      */
	__u32	__pad2;		/* 20 -- brings bar[] to offset 24, 8-aligned  */
	struct ffn_nac_bar_info bar[FFN_NAC_NUM_BARS];	/* 24 */
	/* Which family member bound: "ce40", "ca1", ... NUL-terminated.
	 * Placed AFTER bar[] so the head stays exactly 24 bytes and the
	 * existing offsets do not move. */
	char	variant[FFN_NAC_VARIANT_NAME_MAX];
};

#define FFN_NAC_IOC_MAGIC	'C'
/* Describe the device: identity, BAR geometry, flat-space layout. */
#define FFN_NAC_IOC_INFO	_IOR(FFN_NAC_IOC_MAGIC, 1, struct ffn_nac_info)

#endif /* _FFN_NAC_ABI_H */
