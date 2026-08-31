/*
 * ffn_bcm_abi.h -- userspace ABI for FFN's OCTEON III BCM control driver.
 *
 * This is the ONLY thing userspace needs to talk to the BCM88375 Qumran-AX on
 * the PA-5220's dataplane complex. It replaces the /dev/mem path that
 * ffn_cpdpd used during bring-up.
 *
 * WHY A KERNEL DRIVER AT ALL -- the /dev/mem approach worked, so the reasons
 * have to be concrete:
 *
 *   1. S-channel is a single shared resource with visible state. A message is
 *      "write MSG0..MSGn, set MSG_START, poll MSG_DONE, read MSG0..MSGn".
 *      Two writers interleaving in that window corrupt each other's replies
 *      with no error indication -- the second reader simply gets the first
 *      one's answer. /dev/mem cannot serialise this; a mutex in one driver
 *      can, and does.
 *
 *   2. The BARs get discovered instead of transcribed. ffn_cpdp.h carried
 *      0x11c0100800000 as a literal, and an earlier revision of that header
 *      had the BCM's window labelled FFN_FE100_BAR2 -- wrong chip, and every
 *      LEDUP access read as an FE100 access. pci_resource_start() cannot be
 *      wrong in that way.
 *
 *   3. pci_enable_device() sets PCI_COMMAND_MEMORY properly. Both chips come
 *      out of reset with memory decode OFF, which is why every BAR read
 *      returned 0xffffffff until something wrote the sysfs enable node.
 *
 *   4. It is the place the port/netdev work has to hang off later. DNX init
 *      and PMD firmware load both need ordered, exclusive register access.
 *
 * ENDIANNESS: handled entirely inside the driver. Every value crossing this
 * ABI is in native CPU order -- a u32 you read here is the register value as
 * the Broadcom documentation states it. The CMIC is little-endian and the
 * OCTEON is big-endian, so the driver byte-swaps; callers must not.
 */

#ifndef FFN_BCM_ABI_H
#define FFN_BCM_ABI_H

#include <linux/types.h>
#include <linux/ioctl.h>

#define FFN_BCM_DEVNAME		"ffn_bcm"
#define FFN_BCM_IOC_MAGIC	'B'

/* CMIC S-channel carries at most 16 message words in either direction. */
#define FFN_BCM_SCHAN_NMSG	16
/* LEDUP PROGRAM_RAM and DATA_RAM are 256 bytes each, 4-byte register stride. */
#define FFN_BCM_LED_RAMSZ	256

/* Which BAR an offset is relative to. BAR2 is the CMIC window (S-channel,
 * MIIM, LEDUP); BAR0 is the small 32K config/ident window. */
#define FFN_BCM_BAR_CMIC	2
#define FFN_BCM_BAR_IDENT	0

struct ffn_bcm_reg {
	__u32 bar;		/* FFN_BCM_BAR_CMIC or FFN_BCM_BAR_IDENT */
	__u32 off;		/* byte offset into that BAR, 4-byte aligned */
	__u32 val;		/* WR: value in; RD: value out */
	__u32 count;		/* RD only: 1..FFN_BCM_SCHAN_NMSG, see vals[] */
	__u32 vals[FFN_BCM_SCHAN_NMSG];	/* RD with count>1: the values out */
};

struct ffn_bcm_schan {
	__u32 nsend;		/* message words to send, 1..16 */
	__u32 nrecv;		/* reply words to read back, 0..16 */
	__u32 ctrl;		/* out: final SCHAN_CTRL, raw */
	__u32 spins;		/* out: poll iterations the op took */
	/*
	 * out: the error bits that were ALREADY latched in SCHAN_CTRL when this
	 * message started, and so are not this message's fault.
	 *
	 * They persist: measured on a BCM88375_A0, writing 0 to SCHAN_CTRL
	 * clears MSG_DONE but leaves SCHAN_ERROR standing, and neither writing
	 * the bit back nor asserting ABORT clears it either (SCHAN_ERR at
	 * 0x10008 reads 0 throughout). So after any failed message, ctrl keeps
	 * reporting that error indefinitely, and the driver judges success on
	 * (ctrl & ~pre_err). A caller decoding ctrl by itself must subtract
	 * pre_err or it will blame every later message for the first failure.
	 */
	__u32 pre_err;
	__u32 msg[FFN_BCM_SCHAN_NMSG];	/* in: send words / out: reply words */
};

struct ffn_bcm_led {
	__u32 unit;		/* LEDUP unit; 0 is the front-panel processor */
	__u32 len;		/* LOAD: program bytes, 1..256 */
	__u32 enable;		/* EN: nonzero sets LEDUP_EN, zero clears it */
	__u32 ctrl;		/* out: LEDUP<unit>_CTRL readback */
	__u8  prog[FFN_BCM_LED_RAMSZ];	/* LOAD: the program */
};

struct ffn_bcm_info {
	__u32 vendor;		/* PCI vendor, expect 0x14e4 */
	__u32 device;		/* PCI device, expect 0x8375 */
	__u32 revision;
	__u32 ident;		/* BAR0 reg 0, byte-swapped: low 16 = 0x8375 */
	__u64 bar0_phys;
	__u64 bar2_phys;
	__u32 bar0_len;
	__u32 bar2_len;
	__u64 schan_ops;	/* completed S-channel messages */
	__u64 schan_errs;	/* messages that ended in an error bit */
	__u64 schan_timeouts;	/* messages that never raised MSG_DONE */
	char  pci[16];		/* e.g. "0001:01:00.0" */
};

/* Read one register (count<=1, value in .val) or a run of them (count>1,
 * values in .vals). A run is issued under one lock, so it cannot tear across
 * another caller's S-channel message. */
#define FFN_BCM_IOC_RD		_IOWR(FFN_BCM_IOC_MAGIC, 1, struct ffn_bcm_reg)
#define FFN_BCM_IOC_WR		_IOW (FFN_BCM_IOC_MAGIC, 2, struct ffn_bcm_reg)
/* One complete S-channel transaction, start to reply, under the driver lock. */
#define FFN_BCM_IOC_SCHAN	_IOWR(FFN_BCM_IOC_MAGIC, 3, struct ffn_bcm_schan)
#define FFN_BCM_IOC_LED_LOAD	_IOWR(FFN_BCM_IOC_MAGIC, 4, struct ffn_bcm_led)
#define FFN_BCM_IOC_LED_EN	_IOWR(FFN_BCM_IOC_MAGIC, 5, struct ffn_bcm_led)
#define FFN_BCM_IOC_INFO	_IOR (FFN_BCM_IOC_MAGIC, 6, struct ffn_bcm_info)

#endif /* FFN_BCM_ABI_H */
