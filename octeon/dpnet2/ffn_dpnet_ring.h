/*
 * ffn_dpnet -- FFN-owned virtual Ethernet between the OCTEON CP (CN73XX) and
 * the OCTEON DP (CN78XX), carried over the CP's PCIe link to the DP.
 *
 * WHERE THIS SITS. ffn_pcnet already carries MP <-> CP the same way (rings in
 * CP DRAM, MP reaches them through the BAR1 window). This is the next link
 * down: rings in DP DRAM, the CP reaches them through the DP's BAR1 window.
 * Chained, the DP is reachable from the MP entirely over PCIe.
 *
 *      MP  --pcnet(127.1.1.0/24)-->  CP  --dpnet(127.1.2.0/24)-->  DP
 *
 * WHY POLLED SHARED MEMORY AND NOT THE VENDOR'S pci_dma. The vendor protocol is
 * fully recovered (see DPNET.md) and is the v2 fast path, but it needs three
 * things FFN does not have in place yet: a kernel module on each side, the
 * OCTEON DPI DMA engine on the DP, and MSI-X plumbing. This transport needs
 * none of them and runs on the kernels that are booted today. Notably, the
 * vendor's own HOST side also just memcpys payload through the BAR -- so the
 * data path here is the same shape as theirs, minus the interrupts.
 *
 * REGION. DP phys 0x00600000, 2 MB. The DP has no System RAM below 0x800000
 * (verified in its /proc/iomem), and BAR1 index 1 already maps DP phys
 * 0x400000-0x7FFFFF for the ffn-dpsh mailbox, which uses only the first 64 KB.
 * So this region is unmanaged DRAM inside an already-programmed, already-proven
 * window: no new BAR1 index, no CSR write, nothing new to justify as safe.
 *
 * That leaves the window carved up as follows, all four boundaries on a MB so
 * the `dd if=/dev/mem bs=1M skip=N` read-out recipe works on the DP side:
 *
 *   0x400000  64 KB   ffn-dpsh mailbox        (in use by the shell channel)
 *   0x410000  ~3.9 MB unused tail of that MB
 *   0x500000  1 MB    STAGING SCRATCH         (ffn_dpstage.py; see DPNET.md)
 *   0x600000  2 MB    this region             (rings + header)
 *
 * The staging area exists to solve the bootstrap: the DP needs this daemon's
 * binary before it has any network to fetch it over, so the CP writes it into
 * DP DRAM through the same window and the DP dd's it back out.
 *
 * ENDIANNESS, and the one thing that is genuinely subtle here. Both CPUs are
 * big-endian, so the shared layout is plain big-endian and the DP touches it
 * natively with no conversion at all. The CP, however, reaches it through a
 * PCIe window that reverses the bytes within every aligned 64-bit word. So the
 * CP -- and only the CP -- byte-swaps each 8-byte group on the way in and out.
 * That is exactly what ffn-dpsh does today, and it is why every control field
 * below is padded into its own 8-byte group: a 64-bit group is the atom of this
 * window, so giving each field a private group means no access is ever a
 * read-modify-write of a field the other side owns.
 *
 * SINGLE-WRITER DISCIPLINE. Every 8-byte group has exactly one writer, named in
 * the comments. head is written only by the producer, tail only by the consumer,
 * and the consumer never writes into a slot. That is what makes the rings
 * lock-free without any atomics.
 *
 * COHERENCY. On OCTEON the L2 is the coherence point for both the cores and the
 * IOB, so a CP PCIe write lands in L2 and a DP core's cached read sees it.
 * Producers put a write barrier between the payload and the head advance;
 * consumers read head before the payload. Each frame also carries a CRC32, so a
 * coherency slip is detected rather than acted on.
 */

#ifndef FFN_DPNET_RING_H
#define FFN_DPNET_RING_H

#include <stdint.h>

/* ---- region ------------------------------------------------------------- */

#define FFN_DPNET_DP_PHYS	0x00600000ULL	/* DP physical base of the region */
#define FFN_DPNET_SIZE		0x00200000ULL	/* 2 MB: 0x600000-0x7FFFFF        */

/* Bootstrap staging area, in the same window and deliberately clear of both the
 * ffn-dpsh mailbox and this region. Not touched at run time. */
#define FFN_DPNET_STAGE_PHYS	0x00500000ULL
#define FFN_DPNET_STAGE_SIZE	0x00100000ULL	/* 1 MB */

/* BAR1 index 1 maps DP phys 0x400000-0x7FFFFF at the same offset in the BAR,
 * so for the CP the BAR offset of the region IS its DP physical address. */
#define FFN_DPNET_BAR_OFF	FFN_DPNET_DP_PHYS

#define FFN_DPNET_MAGIC		0x46464E44504E5431ULL	/* "FFNDPNT1" */
#define FFN_DPNET_VERSION	1

/* ---- geometry ----------------------------------------------------------- */

#define FFN_DPNET_NSLOTS	256	/* per ring; MUST be a power of two */
#define FFN_DPNET_SLOT		2048	/* bytes per slot, payload starts at +8 */
#define FFN_DPNET_MTU		1500
#define FFN_DPNET_MAXFRAME	(FFN_DPNET_SLOT - 8)

/* Ring bases, as offsets from the region base. Each ring gets a 1 MB slice so
 * the two never share a page, let alone a cache line. */
#define FFN_DPNET_C2D_OFF	0x001000	/* CP produces, DP consumes */
#define FFN_DPNET_D2C_OFF	0x101000	/* DP produces, CP consumes */

/* ---- region header, at region base + 0. Each field owns its 8-byte group. -- */

#define FFN_DPNET_H_MAGIC	0x00	/* u64  magic                    (CP) */
#define FFN_DPNET_H_VERSION	0x08	/* u32  version                  (CP) */
#define FFN_DPNET_H_NSLOTS	0x0C	/* u32  nslots                   (CP) */
#define FFN_DPNET_H_SLOTSZ	0x10	/* u32  slot_bytes               (CP) */
#define FFN_DPNET_H_PAD0	0x14	/* u32  pad                            */
#define FFN_DPNET_H_C2D_OFF	0x18	/* u32  c2d ring offset          (CP) */
#define FFN_DPNET_H_D2C_OFF	0x1C	/* u32  d2c ring offset          (CP) */
#define FFN_DPNET_H_CP_UP	0x20	/* u32  CP end attached          (CP) */
#define FFN_DPNET_H_GEN		0x28	/* u32  bumped each CP attach    (CP) */
#define FFN_DPNET_H_DP_UP	0x30	/* u32  DP end attached          (DP) */
#define FFN_DPNET_H_DP_GEN	0x38	/* u32  gen the DP has acked     (DP) */
#define FFN_DPNET_HDR_SIZE	0x40

/* ---- per-ring header, at ring base + 0 ---------------------------------- */

#define FFN_DPNET_R_HEAD	0x00	/* u32 monotonic, producer only */
#define FFN_DPNET_R_TAIL	0x08	/* u32 monotonic, consumer only */
#define FFN_DPNET_R_DROPS	0x10	/* u32 ring-full drops, producer only */
#define FFN_DPNET_R_HDR_SIZE	0x40	/* slots start here */

/* ---- per-slot ----------------------------------------------------------- */

/* len and crc share one 8-byte group ON PURPOSE: the producer publishes both in
 * a single 64-bit store, so a consumer can never see a length from this frame
 * with a CRC from the last one. The consumer never writes to a slot at all --
 * head/tail alone say what is ready, so there is no flag to clear. */
#define FFN_DPNET_S_LEN		0x00	/* u32 payload length, producer only */
#define FFN_DPNET_S_CRC		0x04	/* u32 CRC32 of payload, producer only */
#define FFN_DPNET_S_DATA	0x08	/* payload bytes */

/* byte offset of slot i from the ring base */
static inline uint32_t ffn_dpnet_slot_off(uint32_t i)
{
	return FFN_DPNET_R_HDR_SIZE + i * FFN_DPNET_SLOT;
}

/* Total bytes a ring occupies. Must fit the 1 MB slice; the daemon asserts it
 * at compile time rather than trusting the arithmetic. Kept cast-free so it can
 * be evaluated in an #if -- the preprocessor cannot parse a cast. */
#define FFN_DPNET_RING_BYTES \
	(FFN_DPNET_R_HDR_SIZE + FFN_DPNET_NSLOTS * FFN_DPNET_SLOT)

/* ---- link addressing ---------------------------------------------------- */

/* 127.1.2.x, matching pcnet's 127.1.1.x. 127/8 is non-routable, so this link
 * cannot be reached from any physical topology -- the PCIe-only isolation the
 * security constraint requires. */
#define FFN_DPNET_IFNAME	"ffndp0"
#define FFN_DPNET_CP_ADDR	"127.1.2.1"
#define FFN_DPNET_DP_ADDR	"127.1.2.2"
#define FFN_DPNET_NETMASK	"255.255.255.0"

#endif /* FFN_DPNET_RING_H */
