/*
 * ffn_pcnet -- an FFN-owned virtual Ethernet between the Xeon-D (MP) and the
 * OCTEON CP, carried over PCIe.
 *
 * WHY THIS EXISTS, and why it is not the SLI packet ring. The vendor's PCIC
 * makes the OCTEON DMA descriptors out of HOST memory, which requires the
 * OCTEON's outbound PCIe path to reach the host -- a path that has never been
 * made to work from the host side alone, because the host cannot see or
 * configure the OCTEON end of it. This transport inverts that: the shared rings
 * live in OCTEON DRAM, and the HOST reaches them across PCIe through the BAR1
 * window (the same window ffn_octdram/ffn_cpdp already drive reliably). So the
 * only cross-PCIe access is host-initiated, which is the direction that is
 * known to work. Nothing here depends on the OCTEON mastering the bus.
 *
 * This is the "own both ends" transport: FFN writes the code on each side, so
 * the two agree by construction and there is no opaque peer to match.
 *
 * DIRECTION ASYMMETRY, deliberately exploited. A host BAR write is posted and
 * pipelines; a host BAR read serialises and is slow. The bulk NFS flow is
 * MP -> OCTEON (files from the MP's unified storage), which is HOST -> OCTEON,
 * i.e. host writes -- the fast direction. OCTEON -> MP is small requests.
 *
 * ENDIANNESS. Every multi-byte control field in the region is BIG-ENDIAN, as in
 * ffn_cpdp: the OCTEON is big-endian and reads them natively, the host swaps.
 * Packet payload bytes are a byte stream and are not swapped.
 *
 * COHERENCY. On OCTEON the L2 is the coherence point for both cores and the IOB,
 * so a host PCIe write lands in L2 and a cached core mapping sees it. Producers
 * issue a write barrier between payload and the head advance; consumers read
 * head before the payload. Each frame also carries a CRC32, so a coherency slip
 * is detected rather than acted on -- the same belt-and-braces as ffn_cpdp.
 */

#ifndef FFN_PCNET_H
#define FFN_PCNET_H

#include <stdint.h>

/*
 * Region: one 4 MB DRAM segment at 0x29000000, so a single BAR1 index mapping
 * covers the whole thing and the host never has to re-point the window in the
 * steady state. It sits above every other reserved region (kernel 0x21e6, overlay
 * 0x2200, ffn_mem 0x2400, cpdp 0x2800) and outside the kernel's usable RAM map.
 */
#define FFN_PCNET_BASE		0x29000000ULL
#define FFN_PCNET_SIZE		0x00400000ULL	/* 4 MB, one BAR1 segment */

#define FFN_PCNET_MAGIC		0x46464e504e455431ULL	/* "FFNPNET1" */
#define FFN_PCNET_VERSION	1

/*
 * Two rings. Offsets are from FFN_PCNET_BASE. Each ring gets its own 2 MB half
 * so producer and consumer never share a cache line across the head/tail split.
 *
 *   H2O: host produces, OCTEON consumes  (MP -> CP; the bulk direction)
 *   O2H: OCTEON produces, host consumes  (CP -> MP)
 */
#define FFN_PCNET_H2O_OFF	0x001000
#define FFN_PCNET_O2H_OFF	0x200000

#define FFN_PCNET_NSLOTS	256		/* per ring; power of two */
#define FFN_PCNET_SLOT		2048		/* bytes per slot; >= MTU+headroom */
#define FFN_PCNET_MTU		1500

/*
 * Region header, at FFN_PCNET_BASE. Written once by whichever side wins the
 * init race (both check the magic first). Big-endian.
 */
struct ffn_pcnet_hdr {
	uint64_t magic;			/* FFN_PCNET_MAGIC once initialised */
	uint32_t version;		/* FFN_PCNET_VERSION */
	uint32_t nslots;		/* FFN_PCNET_NSLOTS, for cross-checking */
	uint32_t slot_bytes;		/* FFN_PCNET_SLOT */
	uint32_t h2o_off;		/* FFN_PCNET_H2O_OFF */
	uint32_t o2h_off;		/* FFN_PCNET_O2H_OFF */
	uint32_t host_up;		/* host end has attached */
	uint32_t oct_up;		/* OCTEON end has attached */
	uint32_t rsvd[9];		/* pad the header to 64 bytes */
};

/*
 * One ring. head and tail live at the FRONT so a consumer polling for work
 * reads a single cache line. Slots follow. Ring is empty when head == tail and
 * full when (head + 1) % nslots == tail, so one slot is always left unused --
 * the standard single-producer/single-consumer convention that needs no lock.
 *
 * head is written ONLY by the producer, tail ONLY by the consumer.
 */
struct ffn_pcnet_ring {
	uint32_t head;			/* producer's next slot */
	uint32_t tail;			/* consumer's next slot */
	uint32_t producer_drops;	/* ring-full drops, for diagnostics */
	uint32_t rsvd;
	uint8_t  pad[48];		/* keep slots off the head/tail line */
	/* struct ffn_pcnet_slot slots[FFN_PCNET_NSLOTS]; follows here */
};

/*
 * One slot. len doubles as the ready flag on the wire: a producer writes the
 * payload and the CRC first, then len, then advances head; so a consumer that
 * races the head advance still sees len == 0 and waits. len is cleared by the
 * consumer once the frame is taken.
 */
struct ffn_pcnet_slot {
	uint32_t len;			/* payload length; 0 = empty/not-ready */
	uint32_t crc;			/* CRC32 of the payload bytes */
	uint8_t  data[FFN_PCNET_SLOT - 8];
};

#define FFN_PCNET_RING_HDR	64		/* sizeof(ring) header area */

/* byte offset of slot i within a ring */
static inline uint32_t ffn_pcnet_slot_off(uint32_t i)
{
	return FFN_PCNET_RING_HDR + i * FFN_PCNET_SLOT;
}

#endif /* FFN_PCNET_H */
