/*
 * ffn_cpdp.h -- FFN control-plane <-> data-plane transport ABI over PCIe.
 *
 * The x86 control plane programs the OCTEON dataplane through a pair of rings
 * in OCTEON DRAM. Both sides already have a proven path to that memory:
 *
 *   CP side: the SLI/BAR window (tools/ffn_octdram.py). Verified byte-exact --
 *            it has moved a 20 MB cpio with a matching sha256.
 *   DP side: mmap of /dev/mem. Verified -- it is how ffn_mem was loaded into a
 *            running Octeon.
 *
 * No interrupts and no vendor PCIC ring format are involved. This is FFN's own
 * protocol, so it can ship; PAN's pcic.ko layout stays reference material.
 *
 * DESIGN NOTES that matter for correctness:
 *
 * 1. BYTE ORDER IS BIG-ENDIAN for every field here. The OCTEON is big-endian
 *    and x86 is little-endian, so a shared structure needs one defined order or
 *    it silently reads garbage. Big-endian keeps the DP side free of swapping;
 *    the CP side packs with Python's '>' formats.
 *
 * 2. Cache coherency: on OCTEON the L2 is the coherence point for both the
 *    cores and the IOB, so PCIe writes land in L2 and a cached core mapping
 *    sees them. The DP side still issues `sync` around ring accesses, and every
 *    message carries a CRC32 of its payload, so a coherency mistake is
 *    DETECTED rather than silently acted upon.
 *
 * 3. Single producer / single consumer per ring. head is written only by the
 *    producer, tail only by the consumer, so no locking is required.
 *
 * 4. The region sits OUTSIDE the kernel's usable RAM map, so nothing
 *    reallocates it. 0x21000000 (kernel image), 0x22000000 (overlay cpio) and
 *    0x24000000 (ffn_mem) are all already used this way and verified.
 */

#ifndef FFN_CPDP_H
#define FFN_CPDP_H

#define FFN_CPDP_BASE		0x28000000ULL	/* DRAM phys addr of the region */
#define FFN_CPDP_SIZE		0x00100000ULL	/* 1 MB */

#define FFN_CPDP_MAGIC		0x46464e4350445031ULL	/* "FFNCPDP1" */
#define FFN_CPDP_VERSION	1

#define FFN_CPDP_SLOT		4096		/* bytes per slot */
#define FFN_CPDP_NSLOTS		32		/* slots per ring */

/* ring byte offsets from FFN_CPDP_BASE */
#define FFN_CPDP_CP2DP_OFF	0x1000
#define FFN_CPDP_DP2CP_OFF	0x40000

/* ---- operations ------------------------------------------------------- */
#define FFN_OP_PING		1	/* -> a0=version a1=magic  liveness */
#define FFN_OP_INFO		2	/* -> a0=ncores, payload=release str */
#define FFN_OP_MEM_RD		3	/* a0=addr a1=width a2=count -> u64[] */
#define FFN_OP_MEM_WR		4	/* a0=addr a1=width a2=value */
#define FFN_OP_FE100_RD		5	/* a0=BAR2 off a1=count -> u32[] */
#define FFN_OP_FE100_WR		6	/* a0=BAR2 off a1=value */
#define FFN_OP_LINK_GET		7	/* a0=port -> a1=flags a2=carrier */
#define FFN_OP_LINK_SET		8	/* a0=port a1=up */
#define FFN_OP_LED_GET		9	/* a0=LEDUP unit -> a1=CTRL a2=CLK_DIV */
#define FFN_OP_BCM_RD		10	/* a0=BAR2 off a1=count -> u32[] */
#define FFN_OP_BCM_WR		11	/* a0=BAR2 off a1=value */
#define FFN_OP_SCHAN		12	/* a0=nsend a1=nrecv, payload=u32[] */
#define FFN_OP_LED_LOAD		13	/* a0=unit, payload=program bytes */
#define FFN_OP_LED_ENABLE	14	/* a0=unit a1=on -> a1=CTRL readback */
/*
 * Block memory moves. MEM_RD/MEM_WR handle single values; these move a whole
 * payload at once, which is what bulk staging (a bootloader, a kernel, an FPGA
 * bitstream) needs to finish in a sane number of round trips.
 *
 *   MEM_WRBLK  a0=phys addr  a1=len  payload = the bytes to write
 *   MEM_RDBLK  a0=phys addr  a1=len  -> payload = the bytes read
 *
 * len is capped at FFN_CPDP_MAXPAY. Both use 64-bit accesses wherever address
 * and length allow: the PCIe BAR windows on this board do not reliably accept
 * byte-granular writes, while DRAM does not care either way.
 */
#define FFN_OP_MEM_WRBLK	15
#define FFN_OP_MEM_RDBLK	16
/*
 * PCI config-space access, performed by the DP daemon on the CP's own
 * bus. Needed because the DP boot sequence lives partly in config
 * space: D-state, bus master, BAR inspection.
 *
 *   PCI_CFG_RD  a0=offset a1=width(8|16|32)          payload=sysfs path
 *               -> a1 = value
 *   PCI_CFG_WR  a0=offset a1=width a2=value          payload=sysfs path
 *
 * The path is the full /sys/bus/pci/devices/<dddd:bb:dd.f>/config name.
 * Values are plain integers; the daemon handles config space's
 * little-endian byte order.
 */
#define FFN_OP_PCI_CFG_RD	17
#define FFN_OP_PCI_CFG_WR	18

/* ---- status codes ----------------------------------------------------- */
#define FFN_ST_OK		0
#define FFN_ST_BADOP		1
#define FFN_ST_BADARG		2
#define FFN_ST_BADCRC		3
#define FFN_ST_MAPFAIL		4
#define FFN_ST_IOFAIL		5
#define FFN_ST_TOOBIG		6

/*
 * TWO DIFFERENT CHIPS. An earlier version of this header called the BCM's BAR2
 * "FFN_FE100_BAR2", which was simply wrong and made every LEDUP read look like
 * an FE100 access:
 *
 *   0001:01:00.0/.1  14e4:8375  BCM88375 Qumran-AX -- the switch. BAR0 32K,
 *                               BAR2 8M, and BAR2 is where the CMIC window
 *                               (S-channel, MIIM, LEDUP) lives.
 *   0002:01:00.0     feed:fe1c  FE100 -- the PAN session-offload FPGA, BAR0 1M.
 *                               This is what fe100-csr-map describes.
 *
 * Both come out of reset with PCI memory decode OFF, so every BAR read returns
 * 0xffffffff until the enable node is written. The daemon does that at startup.
 *
 * Byte order, for the BCM specifically: its registers are little-endian against
 * the OCTEON's big-endian reads, so the BCM_* ops byte-swap for you. The tell is
 * that BAR0 register 0 reads 0x75830000 raw, which is device id 0x8375 with the
 * halves swapped. Encoding the swap here means callers cannot forget it.
 */
#define FFN_BCM_BAR2		0x11c0100800000ULL	/* CMIC window */
#define FFN_BCM_BAR0		0x11c0101800000ULL
#define FFN_FE100_BAR0		0x11d00f0000000ULL	/* the real FE100 */

/* CMIC S-channel, offsets from FFN_BCM_BAR2. Field positions recovered from
 * bcm.user.dbg's BCM88375_A0 field database -- not guessed. */
#define FFN_SCHAN_CTRL		0x10000
#define FFN_SCHAN_ACK_BEAT	0x10004
#define FFN_SCHAN_ERR		0x10008
#define FFN_SCHAN_MSG0		0x1000c
#define FFN_SCHAN_NMSG		16
#define FFN_SCHAN_MSG_START	(1u << 0)
#define FFN_SCHAN_MSG_DONE	(1u << 1)
#define FFN_SCHAN_ABORT		(1u << 2)
#define FFN_SCHAN_SER_FAIL	(1u << 20)
#define FFN_SCHAN_NACK		(1u << 21)
#define FFN_SCHAN_TIMEOUT	(1u << 22)
#define FFN_SCHAN_ERROR		(1u << 23)
#define FFN_SCHAN_ERRMASK	(FFN_SCHAN_SER_FAIL | FFN_SCHAN_NACK | \
				 FFN_SCHAN_TIMEOUT | FFN_SCHAN_ERROR)

/* LED processor: CMIC_LEDUP<n>_*, LEDUP_EN is bit 0 of CTRL */
#define FFN_LEDUP0_CTRL		0x20000		/* + BAR2 */
#define FFN_LEDUP0_CLK_PARAMS	0x20050
#define FFN_LEDUP0_CLK_DIV	0x2005c
#define FFN_LEDUP0_DATA_RAM	0x20400		/* 256 bytes, 4-byte stride */
#define FFN_LEDUP0_PROG_RAM	0x20800		/* 256 bytes, 4-byte stride */
#define FFN_LEDUP_STRIDE	0x1000		/* LEDUP1 = +0x1000 ... */
#define FFN_LEDUP_EN		(1u << 0)
#define FFN_LEDUP_RAMSZ		256

#ifndef FFN_CPDP_NO_TYPES
typedef unsigned long long ffn_u64;
typedef unsigned int ffn_u32;
typedef unsigned short ffn_u16;
typedef unsigned char ffn_u8;

/* all fields big-endian */
struct ffn_cpdp_super {			/* at FFN_CPDP_BASE */
	ffn_u64 magic;
	ffn_u32 version;
	ffn_u32 slot_size;
	ffn_u32 nslots;
	ffn_u32 flags;
	ffn_u64 cp2dp_off;
	ffn_u64 dp2cp_off;
	ffn_u64 dp_alive;		/* DP increments; CP reads for liveness */
	ffn_u64 dp_boot_id;		/* changes when the DP daemon restarts */
	ffn_u64 reserved[3];
};

struct ffn_cpdp_ring {			/* at BASE + *_off */
	ffn_u32 head;			/* producer only */
	ffn_u32 tail;			/* consumer only */
	ffn_u32 pad[30];		/* slots start 128B in, cacheline clear */
};

struct ffn_cpdp_msg {			/* at the head of each slot */
	ffn_u32 seq;
	ffn_u16 op;
	ffn_u16 status;
	ffn_u32 len;			/* payload bytes after this header */
	ffn_u32 crc;			/* crc32 of the payload */
	ffn_u64 a0;
	ffn_u64 a1;
	ffn_u64 a2;
};

#define FFN_CPDP_HDR		48	/* payload offset; the struct is 40 and
					 * the 8 bytes of slack keep payloads
					 * 8-byte aligned. Both sides use 48. */
#define FFN_CPDP_MAXPAY		(FFN_CPDP_SLOT - FFN_CPDP_HDR)
#endif /* FFN_CPDP_NO_TYPES */

#endif /* FFN_CPDP_H */
