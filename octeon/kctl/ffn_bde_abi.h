/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Copyright (C) 2026 FreeFlow Networks, Inc. */
#ifndef FFN_BDE_ABI_H
#define FFN_BDE_ABI_H

/*
 * The linux-user-bde ioctl interface, as required by the vendor's Broadcom SDK
 * shell (bcm.user).
 *
 * WHY THIS EXISTS
 *
 * bcm.user performs full DNX init of the BCM88375 -- DRAM calibration, fabric,
 * traffic manager, ports, SerDes microcode -- driven from the .soc property
 * files. It is statically linked and RUNS on FFN's own kernel (4.9.57, MIPS64
 * big-endian) on the appliance's CN73XX control-plane Octeon. It stops only
 * here:
 *
 *     open /dev/linux-user-bde: : No such file or directory
 *     ERROR: PCI SOC device probe failed
 *     No attached units.
 *
 * The vendor's own BDE modules exist but are vermagic 3.10.87-oct2-dp and will
 * not load on 4.9.57. So FFN implements the kernel side itself. Doing that lets
 * the vendor stack initialise the switch IN PLACE on the appliance that owns it,
 * while FFN keeps its own kernel and all its tooling -- and lets FFN drive that
 * init repeatedly and observe exactly what it does.
 *
 * PROVENANCE OF WHAT IS IN THIS FILE
 *
 * Everything here was recovered from the CLIENT side. bcm.user.dbg carries full
 * DWARF, so the payload struct did not have to be reversed out of the stripped
 * vendor .ko -- which is where a previous attempt stalled.
 *
 *   - The ioctl encoding is _IO('L', nr), nr in 0..30, i.e. 31 commands. This was
 *     confirmed independently from the vendor .ko's dispatch, which computes
 *     `cmd - 0x20004c00` then bounds-checks against 31.
 *   - lubde_ioctl_t and linux_bde_bus_t below are the DWARF layouts, exact.
 *   - The nr -> command mapping was recovered by scanning bcm.user's .text for
 *     `ori` instructions whose 16-bit immediate is 0x4c00 + nr. That constant is
 *     distinctive enough to be reliable; scanning for small integers is not, and
 *     an earlier attempt at that produced obvious nonsense.
 *
 * NAMING HONESTY
 *
 * Only FOUR real command names survive anywhere in the vendor material, as
 * strings in both bcm.user.dbg and the .ko:
 *
 *     LUBDE_VERSION  LUBDE_GET_NUM_DEVICES  LUBDE_GET_DEVICE  LUBDE_GET_DEVICE_TYPE
 *
 * Every other name below is FFN's, derived from the client function that issues
 * that command. They describe what the command DOES, but they are not the
 * vendor's identifiers, and this file does not pretend otherwise. Commands whose
 * purpose is not established are left explicitly unknown rather than given a
 * confident-sounding guess: a wrong ABI produces a module that makes bcm.user
 * misbehave against live silicon, which is worse than a documented gap.
 */

#include <linux/types.h>
#include <linux/ioctl.h>

#define FFN_BDE_IOC_MAGIC	'L'		/* 0x4c */
#define FFN_BDE_IOC_NR_MAX	30		/* 31 commands, 0..30 */

/*
 * Device nodes, with FIXED majors. The vendor's /sbin/rc creates them with
 * mknod at these numbers rather than reading /proc/devices, so they are not
 * negotiable:
 *
 *     mknod -m 664 /dev/linux-kernel-bde c 127 0
 *     mknod -m 664 /dev/linux-user-bde   c 126 0
 *
 * (Those lines sit next to two commented-out insmods of the vendor modules.
 * BDE is not built into the vendor kernel either -- checked -- so something else
 * in the vendor boot path loaded them.)
 */
#define FFN_BDE_KERNEL_NAME	"linux-kernel-bde"
#define FFN_BDE_KERNEL_MAJOR	127
#define FFN_BDE_USER_NAME	"linux-user-bde"
#define FFN_BDE_USER_MAJOR	126

/*
 * The payload. Exact DWARF layout from bcm.user.dbg, 96 bytes.
 *
 * `dev` selects the device; `rc` is the status the kernel returns. d0..d3 and
 * the dx union carry per-command arguments and results -- which field means what
 * is per-command, see the table below.
 *
 * bde_kernel_addr_t is 8 bytes here (64-bit kernel pointer/physical address).
 */
typedef __u64 ffn_bde_kernel_addr_t;

typedef struct {
	__u32 dev;			/* +0x00 device index */
	__u32 rc;			/* +0x04 status back to userspace */
	__u32 d0;			/* +0x08 */
	__u32 d1;			/* +0x0c */
	__u32 d2;			/* +0x10 */
	__u32 d3;			/* +0x14 */
	ffn_bde_kernel_addr_t p0;	/* +0x18 address-sized argument */
	union {				/* +0x20, 64 bytes */
		__u32 dw[16];
		char  buf[64];
	} dx;
} ffn_lubde_ioctl_t;			/* 96 bytes */

/*
 * Byte-order contract, from DWARF: struct linux_bde_bus_s, 12 bytes.
 *
 * This matters more than its size suggests. The host is MIPS64 BIG-endian and
 * the BCM88375's registers are little-endian relative to it, so the SDK is told
 * explicitly how to swap for each traffic class. Getting this wrong does not
 * fail loudly -- it silently byte-swaps every register access to a live switch.
 * Filled in response to GET_BUS_FEATURES -- which this SDK never issues, so
 * byte order is decided at probe (PAXB_ENDIANESS), not here.
 */
struct ffn_bde_bus {
	int be_pio;			/* programmed I/O accesses */
	int be_packet;			/* packet DMA */
	int be_other;			/* everything else */
};

/*
 * Command numbers -- the vendor's own, from OpenBCM (sdk-6.5.26-DNX.1,
 * systems/bde/linux/user/kernel/linux-user-bde.h). The earlier table here was
 * reconstructed from call sites in a stripped binary and carried confidence tags;
 * it was right about the ones the SDK uses and wrong about 13/14/16 (SPI access
 * and unused, not "interrupt array init"). Names below are LUBDE_* with the
 * FFN_BDE_ prefix; numbers 15-18 have no definition in the vendor header.
 *
 * What a full run through DNX init actually issues: 5, 0, 1, 30, 12, 2, 26, 9,
 * then 6/22 once interrupts are live. 21 (bus features) is never issued, so byte
 * order is not negotiated here -- it is set in hardware at probe.
 */
#define FFN_BDE_VERSION			_IO(FFN_BDE_IOC_MAGIC,  0)
#define FFN_BDE_GET_NUM_DEVICES		_IO(FFN_BDE_IOC_MAGIC,  1)
#define FFN_BDE_GET_DEVICE		_IO(FFN_BDE_IOC_MAGIC,  2)  /* d0 id, d1 rev, d2/d3 CMIC phys */
#define FFN_BDE_PCI_CONFIG_PUT32	_IO(FFN_BDE_IOC_MAGIC,  3)  /* d0 offset, d1 value */
#define FFN_BDE_PCI_CONFIG_GET32	_IO(FFN_BDE_IOC_MAGIC,  4)  /* d0 offset -> d1 */
#define FFN_BDE_GET_DMA_INFO		_IO(FFN_BDE_IOC_MAGIC,  5)  /* d3:d0 dma_pbase, d1 size,
								     * d2 mmap-via-kernel-bde,
								     * dx.dw[1]:dw[0] cpu_pbase */
#define FFN_BDE_ENABLE_INTERRUPTS	_IO(FFN_BDE_IOC_MAGIC,  6)
#define FFN_BDE_DISABLE_INTERRUPTS	_IO(FFN_BDE_IOC_MAGIC,  7)
#define FFN_BDE_USLEEP			_IO(FFN_BDE_IOC_MAGIC,  8)
#define FFN_BDE_WAIT_FOR_INTERRUPT	_IO(FFN_BDE_IOC_MAGIC,  9)  /* BLOCKS until one arrives */
#define FFN_BDE_SEM_OP			_IO(FFN_BDE_IOC_MAGIC, 10)
#define FFN_BDE_UDELAY			_IO(FFN_BDE_IOC_MAGIC, 11)
#define FFN_BDE_GET_DEVICE_TYPE		_IO(FFN_BDE_IOC_MAGIC, 12)  /* d0 = dev_type */
#define FFN_BDE_SPI_READ_REG		_IO(FFN_BDE_IOC_MAGIC, 13)
#define FFN_BDE_SPI_WRITE_REG		_IO(FFN_BDE_IOC_MAGIC, 14)
#define FFN_BDE_READ_REG_16BIT_BUS	_IO(FFN_BDE_IOC_MAGIC, 19)
#define FFN_BDE_WRITE_REG_16BIT_BUS	_IO(FFN_BDE_IOC_MAGIC, 20)
#define FFN_BDE_GET_BUS_FEATURES	_IO(FFN_BDE_IOC_MAGIC, 21)  /* fills struct ffn_bde_bus;
								     * never issued by this SDK */
#define FFN_BDE_WRITE_IRQ_MASK		_IO(FFN_BDE_IOC_MAGIC, 22)  /* store d1 at CMIC offset d0 */
#define FFN_BDE_CPU_WRITE_REG		_IO(FFN_BDE_IOC_MAGIC, 23)
#define FFN_BDE_CPU_READ_REG		_IO(FFN_BDE_IOC_MAGIC, 24)
#define FFN_BDE_CPU_PCI_REGISTER	_IO(FFN_BDE_IOC_MAGIC, 25)
#define FFN_BDE_DEV_RESOURCE		_IO(FFN_BDE_IOC_MAGIC, 26)  /* d0 rsrc (0 CMIC, 1 iProc)
								     * -> d3:d2 phys, d1 size */
#define FFN_BDE_IPROC_READ_REG		_IO(FFN_BDE_IOC_MAGIC, 27)
#define FFN_BDE_IPROC_WRITE_REG		_IO(FFN_BDE_IOC_MAGIC, 28)
#define FFN_BDE_ATTACH_INSTANCE		_IO(FFN_BDE_IOC_MAGIC, 29)
#define FFN_BDE_GET_DEVICE_STATE	_IO(FFN_BDE_IOC_MAGIC, 30)
#define FFN_BDE_REPROBE			_IO(FFN_BDE_IOC_MAGIC, 31)
/* 32..37 are EDK (embedded-core) interrupt/instance/DMA commands; not used here. */

/* Older spellings, kept so nothing that used them breaks. */
#define FFN_BDE_OPEN_UNK26		FFN_BDE_DEV_RESOURCE
#define FFN_BDE_IRQ_MASK_SET		FFN_BDE_WRITE_IRQ_MASK
#define FFN_BDE_CPU_WRITE		FFN_BDE_CPU_WRITE_REG
#define FFN_BDE_CPU_READ		FFN_BDE_CPU_READ_REG
#define FFN_BDE_IPROC_IHOST_READ	FFN_BDE_IPROC_READ_REG
#define FFN_BDE_IPROC_IHOST_WRITE	FFN_BDE_IPROC_WRITE_REG
#define FFN_BDE_BUS_FEATURES		FFN_BDE_GET_BUS_FEATURES
#define FFN_BDE_INSTANCE_ATTACH		FFN_BDE_ATTACH_INSTANCE
#define FFN_BDE_GET_DEV_STATE		FFN_BDE_GET_DEVICE_STATE

/*
 * dev_type bits (include/ibde.h, include/sal/types.h). The one that mattered:
 * BDE_NO_IPROC makes the client bypass iProc windowing and index BAR0 with raw
 * iProc addresses -- it must be CLEAR for this device.
 */
#define FFN_BDE_DEV_PCI			0x00000001
#define FFN_BDE_DEV_BUS_MSI		0x00008000
#define FFN_BDE_DEV_BYTE_SWAP		0x01000000
#define FFN_BDE_DEV_NO_IPROC		0x02000000
#define FFN_BDE_DEV_8MB_REG_SPACE	0x10000000
#define FFN_BDE_DEV_256K_REG_SPACE	0x20000000

/*
 * The device this module binds. Same part ffn_bcm already drives, so the two must
 * not both claim it -- see ffn_bde.c for how that is arbitrated.
 */
#define FFN_BDE_PCI_VENDOR_BROADCOM	0x14e4
#define FFN_BDE_PCI_DEVICE_BCM88375	0x8375

#endif /* FFN_BDE_ABI_H */
