/*
 * ffn_dpnetd -- both ends of the FFN CP <-> DP virtual Ethernet.
 *
 * One binary, two roles, so the two sides of the protocol cannot drift apart:
 *
 *   --role cp   run on the OCTEON CP. Reaches the rings in DP DRAM through the
 *               DP's PCIe BAR1 index-1 window (sysfs resource2). Owns region
 *               init. Byte-swaps every 8-byte group, because that window
 *               reverses bytes within each aligned 64-bit word.
 *
 *   --role dp   run on the OCTEON DP. Reaches the same rings as plain local
 *               DRAM through /dev/mem. No swapping at all: the layout is
 *               big-endian and so is the DP.
 *
 * Each side bridges a TAP interface to the rings. See ffn_dpnet_ring.h for the
 * region layout and the single-writer rules that make the rings lock-free.
 *
 * Build: static big-endian MIPS64 (see Makefile). Runs on both OCTEONs.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>

#include <arpa/inet.h>
#include <netinet/in.h>
/* net/if.h, NOT linux/if.h: the two define struct ifreq incompatibly and
 * including both fails to compile. linux/if_tun.h brings TUNSETIFF/IFF_TAP. */
#include <net/if.h>
#include <linux/if_tun.h>
#include <linux/sockios.h>

#include "ffn_dpnet_ring.h"

/* The ring slice is 1 MB (C2D at 0x001000, D2C at 0x101000). Prove the slots
 * fit rather than trusting the arithmetic. */
#if FFN_DPNET_RING_BYTES > 0x100000
#error "ring does not fit its 1 MB slice: reduce FFN_DPNET_NSLOTS or FFN_DPNET_SLOT"
#endif
#if (FFN_DPNET_NSLOTS & (FFN_DPNET_NSLOTS - 1)) != 0
#error "FFN_DPNET_NSLOTS must be a power of two"
#endif

/* The ffn-dpsh mailbox sits at DP phys 0x400000 inside the same BAR1 window.
 * Its magic is our canary that the window is programmed and the endpoint is
 * awake -- see window_ok(). */
#define DPSH_BAR_OFF	0x400000ULL
#define DPSH_MAGIC_U64	0x46464E4450534832ULL	/* "FFNDPSH2" */

#define DEFAULT_PCI	"0003:03:00.0"
#define SYSFS_PCI	"/sys/bus/pci/devices"

/* ------------------------------------------------------------------ logging */

static int verbose;

static void logmsg(const char *fmt, ...)
{
	va_list ap;
	char ts[32];
	time_t now = time(NULL);
	struct tm tm;

	gmtime_r(&now, &tm);
	strftime(ts, sizeof ts, "%H:%M:%S", &tm);
	fprintf(stderr, "[%s] ffn_dpnetd: ", ts);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	fflush(stderr);
}

static void die(const char *fmt, ...)
{
	va_list ap;

	fputs("ffn_dpnetd: ", stderr);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	exit(1);
}

/* -------------------------------------------------------------------- crc32 */

/* Standard IEEE/zlib CRC32: reflected, poly 0xEDB88320. Both sides compute it
 * over the same payload bytes, so a window coherency slip or a torn frame is
 * detected instead of being handed to the kernel. */
static uint32_t crc_tab[256];

static void crc_init(void)
{
	uint32_t i, j, c;

	for (i = 0; i < 256; i++) {
		c = i;
		for (j = 0; j < 8; j++)
			c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
		crc_tab[i] = c;
	}
}

static uint32_t crc32b(const uint8_t *p, uint32_t n)
{
	uint32_t c = 0xFFFFFFFFu;

	while (n--)
		c = crc_tab[(c ^ *p++) & 0xFF] ^ (c >> 8);
	return c ^ 0xFFFFFFFFu;
}

/* ------------------------------------------------------- region access ----- */

/*
 * A mapped view of the shared region. `swap` is the whole difference between
 * the two roles: the CP reaches the region through a PCIe window that reverses
 * bytes within each aligned 64-bit word, the DP reaches it as local DRAM.
 *
 * Everything goes through 64-bit groups, which is the atom this window deals
 * in. Combined with the layout's one-writer-per-group rule, that means a
 * read-modify-write of a group is safe: nobody else ever writes it.
 */
struct rgn {
	volatile uint8_t *map;	/* start of the mapping */
	size_t		 maplen;
	uint32_t	 base;	/* offset of the region within the mapping */
	int		 swap;
};

static inline uint64_t bswap64(uint64_t v)
{
	return __builtin_bswap64(v);
}

static inline uint64_t rgn_rd64(const struct rgn *r, uint32_t off)
{
	uint64_t v = *(volatile uint64_t *)(r->map + r->base + off);

	return r->swap ? bswap64(v) : v;
}

static inline void rgn_wr64(const struct rgn *r, uint32_t off, uint64_t v)
{
	*(volatile uint64_t *)(r->map + r->base + off) = r->swap ? bswap64(v) : v;
}

/* A u32 field lives in one half of its 8-byte group. Both CPUs are big-endian,
 * so offset+0 is the high half and offset+4 the low half. */
static uint32_t rgn_rd32(const struct rgn *r, uint32_t off)
{
	uint64_t g = rgn_rd64(r, off & ~7u);

	return (off & 4u) ? (uint32_t)(g & 0xFFFFFFFFu) : (uint32_t)(g >> 32);
}

static void rgn_wr32(const struct rgn *r, uint32_t off, uint32_t v)
{
	uint32_t grp = off & ~7u;
	uint64_t g = rgn_rd64(r, grp);

	if (off & 4u)
		g = (g & 0xFFFFFFFF00000000ULL) | (uint64_t)v;
	else
		g = (g & 0x00000000FFFFFFFFULL) | ((uint64_t)v << 32);
	rgn_wr64(r, grp, g);
}

/* Publish len and crc together in one 64-bit store, so a consumer can never
 * pair this frame's length with the previous frame's CRC. */
static void rgn_wr_lencrc(const struct rgn *r, uint32_t off, uint32_t len,
			  uint32_t crc)
{
	rgn_wr64(r, off, ((uint64_t)len << 32) | (uint64_t)crc);
}

static void rgn_rd_lencrc(const struct rgn *r, uint32_t off, uint32_t *len,
			  uint32_t *crc)
{
	uint64_t g = rgn_rd64(r, off);

	*len = (uint32_t)(g >> 32);
	*crc = (uint32_t)(g & 0xFFFFFFFFu);
}

/*
 * Bulk payload copy, in whole 64-bit groups. `off` must be 8-aligned (every
 * slot payload is, by construction). `n` is rounded up to a group: the extra
 * bytes are inside the slot and never looked at, which is cheaper and simpler
 * than a read-modify-write of the tail group.
 */
/* dst/src must be 8-byte aligned: these walk the buffer as uint64_t and MIPS64
 * faults on an unaligned 64-bit load or store. */
static void rgn_read(const struct rgn *r, uint32_t off, void *dst, uint32_t n)
{
	uint64_t *d = (uint64_t *)dst;
	uint32_t i;

	for (i = 0; i < n; i += 8)
		*d++ = rgn_rd64(r, off + i);
}

static void rgn_write(const struct rgn *r, uint32_t off, const void *src,
		      uint32_t n)
{
	const uint64_t *s = (const uint64_t *)src;
	uint32_t i;

	for (i = 0; i < n; i += 8)
		rgn_wr64(r, off + i, *s++);
}

#define ROUNDUP8(x)	(((x) + 7u) & ~7u)

/* ------------------------------------------------------------------- rings - */

/*
 * A ring endpoint. Nothing is cached locally: head and tail are always read
 * from the region. That costs one extra access per operation and buys a real
 * property -- when the CP restarts and zeroes the counters, the DP picks that
 * up on its very next look, with no reset handshake to get wrong.
 */
struct ring {
	const struct rgn *r;
	uint32_t	  off;	/* ring base, offset within the region */
};

static uint32_t ring_head(const struct ring *g)
{
	return rgn_rd32(g->r, g->off + FFN_DPNET_R_HEAD);
}

static uint32_t ring_tail(const struct ring *g)
{
	return rgn_rd32(g->r, g->off + FFN_DPNET_R_TAIL);
}

/*
 * Free slots. Used to decide whether to dequeue from the TAP at all, which is
 * the difference between backpressure and loss: a frame left in the kernel's
 * transmit queue gets sent later, a frame read out and then dropped here is
 * gone and has to be retransmitted end to end.
 *
 * A difference beyond the ring size means the peer restarted and zeroed the
 * counters mid-flight. Report no space rather than a huge one, so the caller
 * waits a tick instead of writing at a bogus index.
 */
static uint32_t ring_space(const struct ring *g)
{
	uint32_t d = ring_head(g) - ring_tail(g);

	if (d > FFN_DPNET_NSLOTS)
		return 0;
	return FFN_DPNET_NSLOTS - d;
}

/* Push one frame. Producer side: payload and {len,crc} first, then a barrier,
 * then the head advance -- so a consumer that reads head can never see a slot
 * that is not fully written. */
static int ring_push(const struct ring *g, const uint8_t *buf, uint32_t len)
{
	uint32_t head = ring_head(g);
	uint32_t tail = ring_tail(g);
	uint32_t slot, soff;

	if ((uint32_t)(head - tail) >= FFN_DPNET_NSLOTS)
		return -1;			/* full */

	slot = head & (FFN_DPNET_NSLOTS - 1);
	soff = g->off + ffn_dpnet_slot_off(slot);

	rgn_write(g->r, soff + FFN_DPNET_S_DATA, buf, ROUNDUP8(len));
	rgn_wr_lencrc(g->r, soff + FFN_DPNET_S_LEN, len, crc32b(buf, len));
	__sync_synchronize();
	rgn_wr32(g->r, g->off + FFN_DPNET_R_HEAD, head + 1);
	return 0;
}

static void ring_bump_drops(const struct ring *g)
{
	uint32_t off = g->off + FFN_DPNET_R_DROPS;

	rgn_wr32(g->r, off, rgn_rd32(g->r, off) + 1);
}

/*
 * Pop one frame. Returns the length, 0 when the ring is empty, or -1 when the
 * frame was rejected (bad length or CRC) -- rejected frames are consumed, so a
 * single corrupt slot cannot wedge the ring.
 */
static int ring_pop(const struct ring *g, uint8_t *buf, uint32_t cap)
{
	uint32_t head, tail, slot, soff, len, crc;

	head = ring_head(g);
	tail = ring_tail(g);
	if (head == tail)
		return 0;
	if ((uint32_t)(head - tail) > FFN_DPNET_NSLOTS) {
		/* peer restarted mid-flight: adopt its head and carry on */
		rgn_wr32(g->r, g->off + FFN_DPNET_R_TAIL, head);
		return -1;
	}

	slot = tail & (FFN_DPNET_NSLOTS - 1);
	soff = g->off + ffn_dpnet_slot_off(slot);
	rgn_rd_lencrc(g->r, soff + FFN_DPNET_S_LEN, &len, &crc);

	if (len == 0 || len > FFN_DPNET_MAXFRAME || len > cap) {
		rgn_wr32(g->r, g->off + FFN_DPNET_R_TAIL, tail + 1);
		return -1;
	}
	rgn_read(g->r, soff + FFN_DPNET_S_DATA, buf, ROUNDUP8(len));
	__sync_synchronize();
	rgn_wr32(g->r, g->off + FFN_DPNET_R_TAIL, tail + 1);

	if (crc32b(buf, len) != crc)
		return -1;
	return (int)len;
}

/* --------------------------------------------------------------- mapping --- */

static void map_cp(struct rgn *r, const char *pci)
{
	char path[256];
	size_t len = (size_t)(FFN_DPNET_BAR_OFF + FFN_DPNET_SIZE);
	void *m;
	int fd;

	snprintf(path, sizeof path, "%s/%s/resource2", SYSFS_PCI, pci);
	fd = open(path, O_RDWR);
	if (fd < 0)
		die("open %s: %s", path, strerror(errno));

	/* Map from 0 so the BAR offset is a plain index into the mapping and no
	 * page-offset arithmetic is needed. */
	m = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (m == MAP_FAILED)
		die("mmap %s (%zu bytes): %s", path, len, strerror(errno));
	close(fd);

	r->map = (volatile uint8_t *)m;
	r->maplen = len;
	r->base = (uint32_t)FFN_DPNET_BAR_OFF;
	r->swap = 1;
}

static void map_dp(struct rgn *r)
{
	size_t len = (size_t)FFN_DPNET_SIZE;
	void *m;
	int fd;

	/* O_SYNC gives an uncached mapping. On cnMIPS the L2 is the coherence
	 * point for both the cores and the IOB, so a cached mapping would also
	 * see the CP's PCIe writes -- but uncached removes the question entirely
	 * and this is a management link, not the forwarding path. */
	fd = open("/dev/mem", O_RDWR | O_SYNC);
	if (fd < 0)
		die("open /dev/mem: %s", strerror(errno));
	m = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
		 (off_t)FFN_DPNET_DP_PHYS);
	if (m == MAP_FAILED)
		die("mmap /dev/mem at 0x%llx: %s",
		    (unsigned long long)FFN_DPNET_DP_PHYS, strerror(errno));
	close(fd);

	r->map = (volatile uint8_t *)m;
	r->maplen = len;
	r->base = 0;
	r->swap = 0;
}

/*
 * Is the BAR1 window actually pointed at the DP and is the endpoint awake?
 *
 * The ffn-dpsh mailbox lives at DP phys 0x400000 in this same window and its
 * magic is a known value, so reading it back is a free, non-destructive canary.
 * It catches both classic failures at once: an unprogrammed window, and the
 * sysfs `enable` trap where the BAR reads all-ones because the PLX bridges were
 * never re-walked. Deliberately does not try to fix either -- toggling `enable`
 * underneath a live ffn-dpsh session would break it.
 */
static int window_ok(const struct rgn *r, uint64_t *got)
{
	struct rgn probe = *r;

	probe.base = 0;
	*got = rgn_rd64(&probe, (uint32_t)DPSH_BAR_OFF);
	return *got == DPSH_MAGIC_U64;
}

/* ------------------------------------------------------------------- tap --- */

static void set_addr(int s, const char *ifname, int req, const char *ip)
{
	struct ifreq ifr;
	struct sockaddr_in sin;

	memset(&ifr, 0, sizeof ifr);
	strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
	memset(&sin, 0, sizeof sin);
	sin.sin_family = AF_INET;
	if (inet_pton(AF_INET, ip, &sin.sin_addr) != 1)
		die("bad address %s", ip);
	memcpy(&ifr.ifr_addr, &sin, sizeof sin);
	if (ioctl(s, req, &ifr) < 0)
		die("ioctl 0x%x %s %s: %s", req, ifname, ip, strerror(errno));
}

static void write_sysctl(const char *path, const char *val)
{
	int fd = open(path, O_WRONLY);

	if (fd < 0) {
		logmsg("note: cannot open %s (%s)", path, strerror(errno));
		return;
	}
	if (write(fd, val, strlen(val)) < 0)
		logmsg("note: cannot write %s (%s)", path, strerror(errno));
	close(fd);
}

static int tap_open(const char *ifname, const char *ip, const char *mask)
{
	struct ifreq ifr;
	char path[128];
	int fd, s;

	fd = open("/dev/net/tun", O_RDWR);
	if (fd < 0)
		die("open /dev/net/tun: %s "
		    "(kernel needs CONFIG_TUN and /dev/net/tun must exist)",
		    strerror(errno));

	memset(&ifr, 0, sizeof ifr);
	ifr.ifr_flags = IFF_TAP | IFF_NO_PI;
	strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
	if (ioctl(fd, TUNSETIFF, &ifr) < 0)
		die("TUNSETIFF %s: %s", ifname, strerror(errno));

	s = socket(AF_INET, SOCK_DGRAM, 0);
	if (s < 0)
		die("socket: %s", strerror(errno));

	set_addr(s, ifname, SIOCSIFADDR, ip);
	set_addr(s, ifname, SIOCSIFNETMASK, mask);

	memset(&ifr, 0, sizeof ifr);
	strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
	ifr.ifr_mtu = FFN_DPNET_MTU;
	if (ioctl(s, SIOCSIFMTU, &ifr) < 0)
		die("SIOCSIFMTU %s: %s", ifname, strerror(errno));

	memset(&ifr, 0, sizeof ifr);
	strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
	if (ioctl(s, SIOCGIFFLAGS, &ifr) < 0)
		die("SIOCGIFFLAGS %s: %s", ifname, strerror(errno));
	ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
	if (ioctl(s, SIOCSIFFLAGS, &ifr) < 0)
		die("SIOCSIFFLAGS %s: %s", ifname, strerror(errno));
	close(s);

	/* 127/8 on a non-loopback interface needs route_localnet, which is what
	 * keeps this link non-routable from any physical topology. */
	snprintf(path, sizeof path,
		 "/proc/sys/net/ipv4/conf/%s/route_localnet", ifname);
	write_sysctl(path, "1");

	if (fcntl(fd, F_SETFL, O_NONBLOCK) < 0)
		die("O_NONBLOCK on tap: %s", strerror(errno));
	return fd;
}

/* ---------------------------------------------------------------- region -- */

static void region_init_cp(const struct rgn *r, int reset)
{
	uint64_t magic = rgn_rd64(r, FFN_DPNET_H_MAGIC);
	uint32_t gen = 0;
	uint32_t i;

	if (magic == FFN_DPNET_MAGIC && !reset)
		gen = rgn_rd32(r, FFN_DPNET_H_GEN);

	/* Take the region down before touching geometry, so a DP already
	 * attached stops trusting it rather than reading a half-built header. */
	rgn_wr32(r, FFN_DPNET_H_CP_UP, 0);
	rgn_wr64(r, FFN_DPNET_H_MAGIC, 0);
	__sync_synchronize();

	/* Zero both ring headers. Slots are left alone: len/crc are rewritten
	 * before each use and every consumer checks the CRC, so there is nothing
	 * to gain from clearing 1 MB through a PCIe window. */
	for (i = 0; i < FFN_DPNET_R_HDR_SIZE; i += 8) {
		rgn_wr64(r, FFN_DPNET_C2D_OFF + i, 0);
		rgn_wr64(r, FFN_DPNET_D2C_OFF + i, 0);
	}

	rgn_wr32(r, FFN_DPNET_H_VERSION, FFN_DPNET_VERSION);
	rgn_wr32(r, FFN_DPNET_H_NSLOTS, FFN_DPNET_NSLOTS);
	rgn_wr32(r, FFN_DPNET_H_SLOTSZ, FFN_DPNET_SLOT);
	rgn_wr32(r, FFN_DPNET_H_C2D_OFF, FFN_DPNET_C2D_OFF);
	rgn_wr32(r, FFN_DPNET_H_D2C_OFF, FFN_DPNET_D2C_OFF);
	rgn_wr32(r, FFN_DPNET_H_GEN, gen + 1);
	__sync_synchronize();
	rgn_wr64(r, FFN_DPNET_H_MAGIC, FFN_DPNET_MAGIC);
	__sync_synchronize();
	rgn_wr32(r, FFN_DPNET_H_CP_UP, 1);

	logmsg("region initialised at DP phys 0x%llx, gen %u, %u slots x %u B",
	       (unsigned long long)FFN_DPNET_DP_PHYS, gen + 1,
	       FFN_DPNET_NSLOTS, FFN_DPNET_SLOT);
}

/*
 * The DP waits for the CP's header instead of writing one. Geometry is checked,
 * not assumed: mismatched slot counts on the two sides would silently index
 * different slots, which is the kind of bug that looks like flaky hardware.
 */
static int region_attach_dp(const struct rgn *r, int wait_secs)
{
	int waited = 0;

	for (;;) {
		if (rgn_rd64(r, FFN_DPNET_H_MAGIC) == FFN_DPNET_MAGIC &&
		    rgn_rd32(r, FFN_DPNET_H_CP_UP) == 1) {
			uint32_t ver = rgn_rd32(r, FFN_DPNET_H_VERSION);
			uint32_t ns = rgn_rd32(r, FFN_DPNET_H_NSLOTS);
			uint32_t sz = rgn_rd32(r, FFN_DPNET_H_SLOTSZ);
			uint32_t c2d = rgn_rd32(r, FFN_DPNET_H_C2D_OFF);
			uint32_t d2c = rgn_rd32(r, FFN_DPNET_H_D2C_OFF);
			uint32_t gen = rgn_rd32(r, FFN_DPNET_H_GEN);

			if (ver != FFN_DPNET_VERSION || ns != FFN_DPNET_NSLOTS ||
			    sz != FFN_DPNET_SLOT || c2d != FFN_DPNET_C2D_OFF ||
			    d2c != FFN_DPNET_D2C_OFF) {
				logmsg("geometry mismatch: CP says "
				       "ver=%u slots=%u slot=%u c2d=0x%x d2c=0x%x; "
				       "this build has ver=%u slots=%u slot=%u "
				       "c2d=0x%x d2c=0x%x -- rebuild both sides "
				       "together", ver, ns, sz, c2d, d2c,
				       FFN_DPNET_VERSION, FFN_DPNET_NSLOTS,
				       FFN_DPNET_SLOT, FFN_DPNET_C2D_OFF,
				       FFN_DPNET_D2C_OFF);
				return -1;
			}
			rgn_wr32(r, FFN_DPNET_H_DP_GEN, gen);
			__sync_synchronize();
			rgn_wr32(r, FFN_DPNET_H_DP_UP, 1);
			logmsg("attached to CP region, gen %u", gen);
			return 0;
		}
		if (wait_secs >= 0 && waited >= wait_secs) {
			logmsg("no CP region after %ds (magic=0x%llx cp_up=%u) "
			       "-- start the CP side first", waited,
			       (unsigned long long)rgn_rd64(r, FFN_DPNET_H_MAGIC),
			       rgn_rd32(r, FFN_DPNET_H_CP_UP));
			return -1;
		}
		sleep(1);
		waited++;
	}
}

static void print_status(const struct rgn *r)
{
	struct ring c2d = { r, FFN_DPNET_C2D_OFF };
	struct ring d2c = { r, FFN_DPNET_D2C_OFF };

	printf("region      DP phys 0x%llx (%llu KB)\n",
	       (unsigned long long)FFN_DPNET_DP_PHYS,
	       (unsigned long long)FFN_DPNET_SIZE / 1024);
	printf("magic       0x%016llx %s\n",
	       (unsigned long long)rgn_rd64(r, FFN_DPNET_H_MAGIC),
	       rgn_rd64(r, FFN_DPNET_H_MAGIC) == FFN_DPNET_MAGIC ? "(ok)"
								: "(NOT INITIALISED)");
	printf("version     %u   slots %u   slot %u B   gen %u\n",
	       rgn_rd32(r, FFN_DPNET_H_VERSION), rgn_rd32(r, FFN_DPNET_H_NSLOTS),
	       rgn_rd32(r, FFN_DPNET_H_SLOTSZ), rgn_rd32(r, FFN_DPNET_H_GEN));
	printf("cp_up       %u        dp_up %u (acked gen %u)\n",
	       rgn_rd32(r, FFN_DPNET_H_CP_UP), rgn_rd32(r, FFN_DPNET_H_DP_UP),
	       rgn_rd32(r, FFN_DPNET_H_DP_GEN));
	printf("c2d ring    head %-10u tail %-10u used %-4u drops %u\n",
	       ring_head(&c2d), ring_tail(&c2d),
	       (uint32_t)(ring_head(&c2d) - ring_tail(&c2d)),
	       rgn_rd32(r, FFN_DPNET_C2D_OFF + FFN_DPNET_R_DROPS));
	printf("d2c ring    head %-10u tail %-10u used %-4u drops %u\n",
	       ring_head(&d2c), ring_tail(&d2c),
	       (uint32_t)(ring_head(&d2c) - ring_tail(&d2c)),
	       rgn_rd32(r, FFN_DPNET_D2C_OFF + FFN_DPNET_R_DROPS));
}

/* -------------------------------------------------------------------- run -- */

static volatile sig_atomic_t want_stats;
static volatile sig_atomic_t want_stop;

static void on_usr1(int sig) { (void)sig; want_stats = 1; }
static void on_term(int sig) { (void)sig; want_stop = 1; }

struct stats {
	uint64_t tx_frames, tx_bytes, tx_full, tx_stall;
	uint64_t rx_frames, rx_bytes, rx_bad, tap_drop;
};

static void run(int tapfd, const struct ring *tx, const struct ring *rx,
		const char *who)
{
	/* 8-aligned because rgn_read/rgn_write walk this buffer as uint64_t.
	 * MIPS64 faults on an unaligned 64-bit access, and the standard does
	 * not promise any particular alignment for a uint8_t array -- today's
	 * compiler happens to align it, which is exactly the kind of thing
	 * that breaks silently on a toolchain change. Size is a whole number
	 * of 8-byte groups too, so rounding a frame length up stays in bounds. */
	uint8_t buf[FFN_DPNET_SLOT] __attribute__((aligned(8)));
	struct stats st;
	struct pollfd pfd;
	int idle = 0, tx_blocked = 0;

	memset(&st, 0, sizeof st);
	pfd.fd = tapfd;
	pfd.events = POLLIN;

	logmsg("%s side running: %s <-> rings at DP phys 0x%llx", who,
	       FFN_DPNET_IFNAME, (unsigned long long)FFN_DPNET_DP_PHYS);

	while (!want_stop) {
		int busy = 0, n, timeout;

		/* region -> tap */
		for (;;) {
			n = ring_pop(rx, buf, sizeof buf);
			if (n == 0)
				break;
			busy = 1;
			if (n < 0) {
				st.rx_bad++;
				continue;
			}
			if (write(tapfd, buf, (size_t)n) != n)
				st.tap_drop++;
			else {
				st.rx_frames++;
				st.rx_bytes += (uint64_t)n;
			}
		}

		/*
		 * tap -> region. Check for space BEFORE reading: an unread
		 * frame stays queued in the kernel and goes out a moment later,
		 * whereas a frame read here and then dropped is real loss that
		 * TCP has to retransmit end to end.
		 */
		for (;;) {
			ssize_t got;

			if (ring_space(tx) == 0) {
				if (!tx_blocked) {
					tx_blocked = 1;
					st.tx_stall++;
				}
				break;
			}
			tx_blocked = 0;
			got = read(tapfd, buf, FFN_DPNET_MAXFRAME);
			if (got <= 0)
				break;
			busy = 1;
			if (ring_push(tx, buf, (uint32_t)got) < 0) {
				/* Should be unreachable given the check above;
				 * counted rather than trusted. */
				ring_bump_drops(tx);
				st.tx_full++;
				continue;
			}
			st.tx_frames++;
			st.tx_bytes += (uint64_t)got;
		}

		if (want_stats) {
			want_stats = 0;
			logmsg("tx %llu frames / %llu B (stalls %llu, dropped %llu)  "
			       "rx %llu frames / %llu B (bad %llu, tap-drop %llu)",
			       (unsigned long long)st.tx_frames,
			       (unsigned long long)st.tx_bytes,
			       (unsigned long long)st.tx_stall,
			       (unsigned long long)st.tx_full,
			       (unsigned long long)st.rx_frames,
			       (unsigned long long)st.rx_bytes,
			       (unsigned long long)st.rx_bad,
			       (unsigned long long)st.tap_drop);
		}

		/*
		 * The inbound ring is not pollable, so this timeout is what
		 * bounds inbound latency. Spin briefly after activity, then
		 * settle to 2 ms -- on the CP that is ~500 eight-byte BAR reads
		 * a second, which is nothing, and it keeps the link responsive.
		 */
		if (busy)
			idle = 0;
		else if (idle < 10000)
			idle++;
		timeout = busy ? 0 : (idle < 200 ? 0 : (idle < 1000 ? 1 : 2));
		if (tx_blocked) {
			/* The tap fd is readable and will stay readable, so
			 * watching it would spin. Wait on the clock instead --
			 * what unblocks us is the peer draining the ring. */
			pfd.events = 0;
			if (timeout < 1)
				timeout = 1;
		} else {
			pfd.events = POLLIN;
		}
		poll(&pfd, 1, timeout);
	}
	logmsg("%s side stopping", who);
}

/* ------------------------------------------------------------------- main -- */

static void usage(void)
{
	fprintf(stderr,
"ffn_dpnetd -- FFN CP <-> DP virtual Ethernet over PCIe\n"
"\n"
"  --role cp|dp        which end to be (required)\n"
"  --status            print region state and exit\n"
"  --reset             cp only: rebuild the region header even if it looks sane\n"
"  --pci BDF           cp only: DP PCI device (default %s)\n"
"  --wait SECS         dp only: seconds to wait for the CP region, -1 = forever\n"
"                      (default 60)\n"
"  --no-tap            attach to the region but do not create the interface\n"
"  --addr IP           override this end's address\n"
"  -v                  verbose\n"
"\n"
"CP end is %s, DP end is %s, interface %s.\n",
		DEFAULT_PCI, FFN_DPNET_CP_ADDR, FFN_DPNET_DP_ADDR,
		FFN_DPNET_IFNAME);
	exit(2);
}

int main(int argc, char **argv)
{
	const char *role = NULL, *pci = DEFAULT_PCI, *addr = NULL;
	int status = 0, reset = 0, no_tap = 0, wait_secs = 60;
	struct rgn r;
	struct ring tx, rx;
	uint64_t canary;
	int is_cp, tapfd = -1, i;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--role") && i + 1 < argc)
			role = argv[++i];
		else if (!strcmp(argv[i], "--pci") && i + 1 < argc)
			pci = argv[++i];
		else if (!strcmp(argv[i], "--addr") && i + 1 < argc)
			addr = argv[++i];
		else if (!strcmp(argv[i], "--wait") && i + 1 < argc)
			wait_secs = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--status"))
			status = 1;
		else if (!strcmp(argv[i], "--reset"))
			reset = 1;
		else if (!strcmp(argv[i], "--no-tap"))
			no_tap = 1;
		else if (!strcmp(argv[i], "-v"))
			verbose = 1;
		else
			usage();
	}
	if (!role || (strcmp(role, "cp") && strcmp(role, "dp")))
		usage();
	is_cp = !strcmp(role, "cp");

	crc_init();

	if (is_cp) {
		map_cp(&r, pci);
		if (!window_ok(&r, &canary))
			die("BAR1 window canary failed: read 0x%016llx at BAR "
			    "offset 0x%llx, expected the ffn-dpsh magic "
			    "0x%016llx.\n"
			    "  all-ones means the sysfs `enable` trap (the PLX "
			    "bridges were not re-walked);\n"
			    "  anything else means index 1 is not pointed at DP "
			    "phys 0x400000.\n"
			    "  Run `ffn-dpsh --status` first -- it programs the "
			    "window and toggles enable.",
			    (unsigned long long)canary,
			    (unsigned long long)DPSH_BAR_OFF,
			    (unsigned long long)DPSH_MAGIC_U64);
		if (verbose)
			logmsg("window canary ok (ffn-dpsh magic readable)");
	} else {
		map_dp(&r);
	}

	if (status) {
		print_status(&r);
		return 0;
	}

	if (is_cp)
		region_init_cp(&r, reset);
	else if (region_attach_dp(&r, wait_secs) < 0)
		return 1;

	if (is_cp) {
		tx.r = &r; tx.off = FFN_DPNET_C2D_OFF;
		rx.r = &r; rx.off = FFN_DPNET_D2C_OFF;
	} else {
		tx.r = &r; tx.off = FFN_DPNET_D2C_OFF;
		rx.r = &r; rx.off = FFN_DPNET_C2D_OFF;
	}

	if (!no_tap) {
		if (!addr)
			addr = is_cp ? FFN_DPNET_CP_ADDR : FFN_DPNET_DP_ADDR;
		tapfd = tap_open(FFN_DPNET_IFNAME, addr, FFN_DPNET_NETMASK);
		logmsg("%s up at %s/24 mtu %d", FFN_DPNET_IFNAME, addr,
		       FFN_DPNET_MTU);
	} else {
		logmsg("--no-tap: region attached, no interface created");
		return 0;
	}

	signal(SIGUSR1, on_usr1);
	signal(SIGTERM, on_term);
	signal(SIGINT, on_term);
	signal(SIGPIPE, SIG_IGN);

	run(tapfd, &tx, &rx, is_cp ? "CP" : "DP");

	if (is_cp)
		rgn_wr32(&r, FFN_DPNET_H_CP_UP, 0);
	else
		rgn_wr32(&r, FFN_DPNET_H_DP_UP, 0);
	return 0;
}
