/*
 * ffn_cpdpd -- FFN dataplane-side transport daemon. Freestanding MIPS64, no libc.
 *
 * Polls the CP->DP ring in OCTEON DRAM, executes the request, and posts a reply
 * on the DP->CP ring. See ffn_cpdp.h for the ABI and the reasoning behind
 * big-endian fields, the CRC, and the choice of shared memory over the vendor
 * PCIC format.
 *
 * Freestanding because the vendor busybox here has no shell and the SDK
 * toolchain has no libc.a -- and because a binary that depends on nothing is
 * one FFN can ship.
 */

#include "ffn_cpdp.h"

#define NR_read		5000
#define NR_write	5001
#define NR_open		5002
#define NR_close	5003
#define NR_mmap		5009
#define NR_munmap	5011
#define NR_ioctl	5015
#define NR_pread64	5016
#define NR_pwrite64	5017
#define NR_nanosleep	5034
#define NR_socket	5040
#define NR_uname	5061
#define NR_exit_group	5205

#define O_RDONLY	0
#define O_RDWR		2
#define PROT_READ	1
#define PROT_WRITE	2
#define MAP_SHARED	1

#define SIOCGIFFLAGS	0x8913
#define SIOCSIFFLAGS	0x8914
#define IFF_UP		0x1

typedef ffn_u64 u64;
typedef ffn_u32 u32;
typedef ffn_u16 u16;
typedef ffn_u8 u8;

static long sys6(long n, long a, long b, long c, long d, long e, long f)
{
	register long r2 __asm__("$2");
	register long r4 __asm__("$4") = a;
	register long r5 __asm__("$5") = b;
	register long r6 __asm__("$6") = c;
	register long r7 __asm__("$7") = d;
	register long r8 __asm__("$8") = e;
	register long r9 __asm__("$9") = f;
	__asm__ __volatile__(
		"daddu $2,$0,%2 ; syscall"
		: "=&r"(r2), "+r"(r7)
		: "ir"(n), "r"(r4), "r"(r5), "r"(r6), "r"(r8), "r"(r9)
		: "$1", "$3", "$10", "$11", "$12", "$13", "$14", "$15",
		  "$24", "$25", "hi", "lo", "memory");
	return r7 ? -r2 : r2;
}

#define sys1(n, a)		sys6(n, (long)(a), 0, 0, 0, 0, 0)
#define sys2(n, a, b)		sys6(n, (long)(a), (long)(b), 0, 0, 0, 0)
#define sys3(n, a, b, c)	sys6(n, (long)(a), (long)(b), (long)(c), 0, 0, 0)

/* MIPS sync: order our ring writes against what the far side observes */
#define barrier()	__asm__ __volatile__("sync" ::: "memory")

static u64 slen(const char *s)
{
	u64 n = 0;

	while (s[n])
		n++;
	return n;
}

static void say(const char *s)
{
	sys3(NR_write, 1, s, slen(s));
}

static void sayhex(u64 v, int digits)
{
	char b[20];
	int i;

	for (i = 0; i < digits; i++)
		b[digits - 1 - i] = "0123456789abcdef"[(v >> (i * 4)) & 0xf];
	b[digits] = 0;
	say(b);
}

/* reflected crc32, matches zlib.crc32 so the CP side can use it directly */
static u32 crc32(const u8 *p, u64 n)
{
	u32 c = 0xffffffffu;
	u64 i;
	int k;

	for (i = 0; i < n; i++) {
		c ^= p[i];
		for (k = 0; k < 8; k++)
			c = (c >> 1) ^ (0xEDB88320u & (u32)(-(int)(c & 1)));
	}
	return ~c;
}

/* ---- big-endian accessors. The OCTEON is big-endian, so these are plain. -- */
static u32 rd32(const volatile void *p) { return *(const volatile u32 *)p; }
static u64 rd64(const volatile void *p) { return *(const volatile u64 *)p; }
static void wr32(volatile void *p, u32 v) { *(volatile u32 *)p = v; }
static void wr64(volatile void *p, u64 v) { *(volatile u64 *)p = v; }

static int memfd = -1;
static volatile u8 *region;		/* the mapped transport region */

/* map a 64K window of physical memory; used for the MEM_* and FE100_* ops */
static volatile void *phys_win(u64 addr, u64 *off)
{
	u64 base = addr & ~0xffffULL;
	long r = sys6(NR_mmap, 0, 0x10000, PROT_READ | PROT_WRITE, MAP_SHARED,
		      memfd, (long)base);

	if (r < 0 && r > -4096)
		return 0;
	*off = addr - base;
	return (volatile void *)r;
}

static void phys_unwin(volatile void *p)
{
	sys6(NR_munmap, (long)p, 0x10000, 0, 0, 0, 0);
}

static int phys_read(u64 addr, int width, u64 *out)
{
	u64 off;
	volatile void *m = phys_win(addr, &off);

	if (!m)
		return FFN_ST_MAPFAIL;
	if (width == 8)
		*out = *(volatile u8 *)((char *)m + off);
	else if (width == 16)
		*out = *(volatile u16 *)((char *)m + off);
	else if (width == 32)
		*out = *(volatile u32 *)((char *)m + off);
	else if (width == 64)
		*out = *(volatile u64 *)((char *)m + off);
	else {
		phys_unwin(m);
		return FFN_ST_BADARG;
	}
	phys_unwin(m);
	return FFN_ST_OK;
}

static int phys_write(u64 addr, int width, u64 val)
{
	u64 off;
	volatile void *m = phys_win(addr, &off);

	if (!m)
		return FFN_ST_MAPFAIL;
	if (width == 8)
		*(volatile u8 *)((char *)m + off) = (u8)val;
	else if (width == 16)
		*(volatile u16 *)((char *)m + off) = (u16)val;
	else if (width == 32)
		*(volatile u32 *)((char *)m + off) = (u32)val;
	else if (width == 64)
		*(volatile u64 *)((char *)m + off) = val;
	else {
		phys_unwin(m);
		return FFN_ST_BADARG;
	}
	phys_unwin(m);
	return FFN_ST_OK;
}

/*
 * PCI config-space read/write through sysfs.
 *
 * pread64/pwrite64 rather than lseek+read: one syscall each, and no shared file
 * offset that two interleaved ops could corrupt.
 *
 * Config space is little-endian on the wire while this daemon runs big-endian,
 * so the bytes are assembled explicitly. Callers pass and receive a plain
 * integer and never see the difference.
 */
static int cfg_access(const volatile u8 *path, u32 pathlen, u64 off, int width,
		      u64 *val, int write)
{
	char p[160];
	u8 buf[8];
	long fd, r;
	u32 i;
	int nb;

	if (!pathlen || pathlen >= sizeof(p))
		return FFN_ST_BADARG;
	if (width == 8)
		nb = 1;
	else if (width == 16)
		nb = 2;
	else if (width == 32)
		nb = 4;
	else
		return FFN_ST_BADARG;
	if (off > 4096 - (u64)nb)
		return FFN_ST_BADARG;

	for (i = 0; i < pathlen; i++)
		p[i] = (char)path[i];
	p[pathlen] = 0;

	fd = sys3(NR_open, p, write ? 2 /*O_RDWR*/ : 0 /*O_RDONLY*/, 0);
	if (fd < 0)
		return FFN_ST_IOFAIL;

	if (write) {
		u64 v = *val;

		for (i = 0; i < (u32)nb; i++)
			buf[i] = (u8)(v >> (8 * i));
		r = sys6(NR_pwrite64, fd, (long)buf, nb, (long)off, 0, 0);
	} else {
		r = sys6(NR_pread64, fd, (long)buf, nb, (long)off, 0, 0);
		if (r == nb) {
			u64 v = 0;

			for (i = 0; i < (u32)nb; i++)
				v |= ((u64)buf[i]) << (8 * i);
			*val = v;
		}
	}
	sys1(NR_close, fd);
	return (r == nb) ? FFN_ST_OK : FFN_ST_IOFAIL;
}

static void bcopy_v(volatile u8 *d, const volatile u8 *s, u64 n)
{
	/*
	 * 64-bit moves wherever alignment allows. This is not just an
	 * optimisation: the PCIe BAR windows this is used against do not
	 * reliably accept byte-granular writes, so a byte-at-a-time copy can
	 * land as garbage on MMIO while looking fine against DRAM. Ragged
	 * head and tail fall back to bytes.
	 */
	while (n && (((unsigned long)d | (unsigned long)s) & 7)) {
		*d++ = *s++;
		n--;
	}
	while (n >= 8) {
		*(volatile u64 *)d = *(const volatile u64 *)s;
		d += 8;
		s += 8;
		n -= 8;
	}
	while (n--)
		*d++ = *s++;
}

/*
 * Copy a block to or from physical memory, mapping one 64 KB window at a time.
 *
 * phys_read/phys_write map and unmap per access, which is fine for a poke and
 * hopeless for bulk. A transport payload is at most FFN_CPDP_MAXPAY (4048)
 * bytes so a transfer straddles at most two windows, but the loop is written
 * for any length.
 */
static int phys_blk(u64 addr, volatile u8 *buf, u64 len, int write)
{
	while (len) {
		u64 off, n;
		volatile void *m = phys_win(addr, &off);

		if (!m)
			return FFN_ST_MAPFAIL;
		n = 0x10000 - off;		/* bytes left in this window */
		if (n > len)
			n = len;
		if (write)
			bcopy_v((volatile u8 *)m + off, buf, n);
		else
			bcopy_v(buf, (volatile u8 *)m + off, n);
		phys_unwin(m);
		addr += n;
		buf += n;
		len -= n;
	}
	return FFN_ST_OK;
}

static u32 bswap32(u32 v)
{
	return ((v & 0xffu) << 24) | ((v & 0xff00u) << 8) |
	       ((v >> 8) & 0xff00u) | ((v >> 24) & 0xffu);
}

static void nap_us(long us);		/* defined below, used by schan polling */

/* ---- BCM88375 CMIC access --------------------------------------------- */
/*
 * The Qumran's registers are little-endian against our big-endian reads, so
 * every 32-bit access is byte-swapped. Proven by BAR0 reg0 reading 0x75830000
 * (= device id 0x8375) and by CMIC_LEDUP0_CLK_DIV reading 100 rather than
 * 0x64000000.
 */
static int bcm_rd(u64 off, u32 *out)
{
	u64 v = 0;
	int st = phys_read(FFN_BCM_BAR2 + off, 32, &v);

	*out = bswap32((u32)v);
	return st;
}

static int bcm_wr(u64 off, u32 val)
{
	return phys_write(FFN_BCM_BAR2 + off, 32, bswap32(val));
}

/*
 * One S-channel transaction. Field positions come from the BCM88375_A0 field
 * database in bcm.user.dbg: MSG_START bit 0, MSG_DONE bit 1, and the error bits
 * SER_CHECK_FAIL/NACK/TIMEOUT/SCHAN_ERROR at 20..23. Poll rather than wait on an
 * interrupt, so no BDE and no kernel module are needed.
 */
static int schan_op(volatile u8 *send, u32 nsend, volatile u8 *recv, u32 nrecv,
		    u64 *ctrl_out)
{
	u32 ctrl = 0;
	u32 i;
	int st;
	int spins;

	if (nsend > FFN_SCHAN_NMSG || nrecv > FFN_SCHAN_NMSG)
		return FFN_ST_BADARG;

	/* clear any stale completion before starting */
	st = bcm_wr(FFN_SCHAN_CTRL, 0);
	if (st != FFN_ST_OK)
		return st;

	for (i = 0; i < nsend; i++) {
		u32 w = rd32((const volatile void *)(send + i * 4));

		st = bcm_wr(FFN_SCHAN_MSG0 + i * 4, w);
		if (st != FFN_ST_OK)
			return st;
	}

	st = bcm_wr(FFN_SCHAN_CTRL, FFN_SCHAN_MSG_START);
	if (st != FFN_ST_OK)
		return st;

	for (spins = 0; spins < 10000; spins++) {
		st = bcm_rd(FFN_SCHAN_CTRL, &ctrl);
		if (st != FFN_ST_OK)
			return st;
		if (ctrl & (FFN_SCHAN_MSG_DONE | FFN_SCHAN_ERRMASK))
			break;
		nap_us(50);
	}
	*ctrl_out = ctrl;

	if (!(ctrl & FFN_SCHAN_MSG_DONE))
		return FFN_ST_IOFAIL;		/* timed out with no completion */

	for (i = 0; i < nrecv; i++) {
		u32 w = 0;

		st = bcm_rd(FFN_SCHAN_MSG0 + i * 4, &w);
		if (st != FFN_ST_OK)
			return st;
		wr32((volatile void *)(recv + i * 4), w);
	}

	bcm_wr(FFN_SCHAN_CTRL, 0);		/* release for the next op */
	return (ctrl & FFN_SCHAN_ERRMASK) ? FFN_ST_IOFAIL : FFN_ST_OK;
}

/*
 * Load the LED processor program. PROGRAM_RAM is 256 byte-wide slots at a
 * 4-byte stride, so one program byte per 32-bit write.
 */
static int led_load(u64 unit, const volatile u8 *prog, u32 len)
{
	u64 base = FFN_LEDUP0_PROG_RAM + unit * FFN_LEDUP_STRIDE;
	u32 i;

	if (len > FFN_LEDUP_RAMSZ)
		return FFN_ST_TOOBIG;
	for (i = 0; i < FFN_LEDUP_RAMSZ; i++) {
		u32 b = (i < len) ? prog[i] : 0;	/* zero-fill the tail */
		int st = bcm_wr(base + i * 4, b);

		if (st != FFN_ST_OK)
			return st;
	}
	return FFN_ST_OK;
}

static int led_enable(u64 unit, int on, u32 *ctrl_out)
{
	u64 off = FFN_LEDUP0_CTRL + unit * FFN_LEDUP_STRIDE;
	u32 v = 0;
	int st = bcm_rd(off, &v);

	if (st != FFN_ST_OK)
		return st;
	if (on)
		v |= FFN_LEDUP_EN;
	else
		v &= ~FFN_LEDUP_EN;
	st = bcm_wr(off, v);
	if (st != FFN_ST_OK)
		return st;
	return bcm_rd(off, ctrl_out);
}

/* ---- link control ------------------------------------------------------ */
struct ifreq_flags {
	char ifr_name[16];
	short ifr_flags;
	char pad[22];
};

static void ifname(char *dst, int port)
{
	dst[0] = 101;			/* e */
	dst[1] = 116;			/* t */
	dst[2] = 104;			/* h */
	dst[3] = (char)(48 + (port & 7));
	dst[4] = 0;
}

static int link_op(int port, int set, int up, u64 *flags_out)
{
	struct ifreq_flags r;
	long s = sys3(NR_socket, 2, 2, 0);
	u64 i;
	int rc = FFN_ST_OK;

	if (s < 0)
		return FFN_ST_IOFAIL;
	for (i = 0; i < sizeof(r); i++)
		((char *)&r)[i] = 0;
	ifname(r.ifr_name, port);

	if (sys3(NR_ioctl, s, SIOCGIFFLAGS, &r) < 0) {
		sys1(NR_close, s);
		return FFN_ST_IOFAIL;
	}
	if (set) {
		if (up)
			r.ifr_flags |= IFF_UP;
		else
			r.ifr_flags &= (short)~IFF_UP;
		if (sys3(NR_ioctl, s, SIOCSIFFLAGS, &r) < 0)
			rc = FFN_ST_IOFAIL;
	}
	*flags_out = (u64)(unsigned short)r.ifr_flags;
	sys1(NR_close, s);
	return rc;
}

static u64 carrier_of(int port)
{
	char path[64];
	char buf[8];
	long fd, n;
	const char *pfx = "/sys/class/net/eth0/carrier";
	u64 i;

	for (i = 0; pfx[i]; i++)
		path[i] = pfx[i];
	path[i] = 0;
	/*
	 * index 18 is the digit in "/sys/class/net/eth0/carrier" -- counting:
	 * / s y s / c l a s s / n e t / e t h 0
	 * 0 1 2 3 4 5 6 7 8 9 ...        17 18
	 * Writing 19 instead clobbers the slash and every open fails.
	 */
	path[18] = (char)(48 + (port & 7));

	fd = sys3(NR_open, path, O_RDONLY, 0);
	if (fd < 0)
		return 0xffffffffffffffffULL;
	n = sys3(NR_read, fd, buf, sizeof(buf) - 1);
	sys1(NR_close, fd);
	if (n <= 0)
		return 0xffffffffffffffffULL;
	return (u64)(buf[0] - 48);
}

/* ---- rings ------------------------------------------------------------- */
static volatile u8 *ring_of(u64 off) { return region + off; }

static volatile u8 *slot_of(volatile u8 *ring, u32 idx)
{
	return ring + 128 + (u64)(idx % FFN_CPDP_NSLOTS) * FFN_CPDP_SLOT;
}

static struct utsname_min {
	char sysname[65], nodename[65], release[65];
	char version[65], machine[65], domainname[65];
} uts;

static u64 ncores(void)
{
	char buf[64];
	long fd, n;
	u64 i;

	fd = sys3(NR_open, "/sys/devices/system/cpu/online", O_RDONLY, 0);
	if (fd < 0)
		return 0;
	n = sys3(NR_read, fd, buf, sizeof(buf) - 1);
	sys1(NR_close, fd);
	if (n <= 0)
		return 0;
	buf[n] = 0;
	/* "0-7" -> 8 ; "0" -> 1 */
	for (i = 0; buf[i]; i++)
		if (buf[i] == 45)		/* - */
			return (u64)(buf[i + 1] - 48) + 1;
	return 1;
}

static void handle(volatile u8 *req, volatile u8 *rsp)
{
	u32 seq = rd32(req + 0);
	u16 op = *(const volatile u16 *)(req + 4);
	u32 len = rd32(req + 8);
	u32 crc = rd32(req + 12);
	u64 a0 = rd64(req + 16);
	u64 a1 = rd64(req + 24);
	u64 a2 = rd64(req + 32);
	u64 r1 = 0, r2 = 0;
	u32 rlen = 0;
	int st = FFN_ST_OK;
	u64 i = 0;

	if (len > FFN_CPDP_MAXPAY) {
		st = FFN_ST_TOOBIG;
		goto reply;
	}
	if (len && crc32((const u8 *)(req + FFN_CPDP_HDR), len) != crc) {
		st = FFN_ST_BADCRC;
		goto reply;
	}

	switch (op) {
	case FFN_OP_PING:
		r1 = FFN_CPDP_VERSION;
		r2 = FFN_CPDP_MAGIC;
		break;
	case FFN_OP_INFO:
		r1 = ncores();
		if (sys1(NR_uname, &uts) == 0) {
			const char *s = uts.release;

			for (i = 0; s[i] && i < 64; i++)
				*(volatile u8 *)(rsp + FFN_CPDP_HDR + i) =
					(u8)s[i];
			rlen = (u32)i;
		}
		break;
	case FFN_OP_MEM_RD: {
		u64 cnt = a2 ? a2 : 1;
		u64 stride = a1 / 8;

		if (!stride || cnt * 8 > FFN_CPDP_MAXPAY) {
			st = FFN_ST_BADARG;
			break;
		}
		for (i = 0; i < cnt; i++) {
			u64 v = 0;

			st = phys_read(a0 + i * stride, (int)a1, &v);
			if (st != FFN_ST_OK)
				break;
			wr64((volatile void *)(rsp + FFN_CPDP_HDR + i * 8), v);
		}
		rlen = (u32)(i * 8);
		break;
	}
	case FFN_OP_MEM_WR:
		st = phys_write(a0, (int)a1, a2);
		break;
	case FFN_OP_PCI_CFG_RD: {
		u64 v = 0;

		st = cfg_access((const volatile u8 *)(req + FFN_CPDP_HDR), len,
				a0, (int)a1, &v, 0);
		r1 = v;
		break;
	}
	case FFN_OP_PCI_CFG_WR: {
		u64 v = a2;

		st = cfg_access((const volatile u8 *)(req + FFN_CPDP_HDR), len,
				a0, (int)a1, &v, 1);
		break;
	}

	case FFN_OP_MEM_WRBLK:
		/* len is authoritative (it is what the CRC covered); a1 must
		 * agree, so a caller that miscounts is rejected rather than
		 * writing a short block to a live BAR. */
		if (!len || len > FFN_CPDP_MAXPAY || a1 != (u64)len) {
			st = FFN_ST_BADARG;
			break;
		}
		st = phys_blk(a0, (volatile u8 *)(req + FFN_CPDP_HDR),
			      (u64)len, 1);
		break;

	case FFN_OP_MEM_RDBLK:
		if (!a1 || a1 > FFN_CPDP_MAXPAY) {
			st = FFN_ST_BADARG;
			break;
		}
		st = phys_blk(a0, (volatile u8 *)(rsp + FFN_CPDP_HDR), a1, 0);
		if (st == FFN_ST_OK)
			rlen = (u32)a1;
		break;

	case FFN_OP_FE100_RD: {
		u64 cnt = a1 ? a1 : 1;

		if (cnt * 4 > FFN_CPDP_MAXPAY) {
			st = FFN_ST_BADARG;
			break;
		}
		for (i = 0; i < cnt; i++) {
			u64 v = 0;

			st = phys_read(FFN_FE100_BAR0 + a0 + i * 4, 32, &v);
			if (st != FFN_ST_OK)
				break;
			/* device is little-endian against our BE reads */
			wr32((volatile void *)(rsp + FFN_CPDP_HDR + i * 4),
			     bswap32((u32)v));
		}
		rlen = (u32)(i * 4);
		break;
	}
	case FFN_OP_FE100_WR:
		st = phys_write(FFN_FE100_BAR0 + a0, 32, bswap32((u32)a1));
		break;
	case FFN_OP_LINK_GET:
		st = link_op((int)a0, 0, 0, &r1);
		r2 = carrier_of((int)a0);
		break;
	case FFN_OP_LINK_SET:
		st = link_op((int)a0, 1, (int)a1, &r1);
		r2 = carrier_of((int)a0);
		break;
	case FFN_OP_BCM_RD: {
		u64 cnt = a1 ? a1 : 1;

		if (cnt * 4 > FFN_CPDP_MAXPAY) {
			st = FFN_ST_BADARG;
			break;
		}
		for (i = 0; i < cnt; i++) {
			u32 v = 0;

			st = bcm_rd(a0 + i * 4, &v);
			if (st != FFN_ST_OK)
				break;
			wr32((volatile void *)(rsp + FFN_CPDP_HDR + i * 4), v);
		}
		rlen = (u32)(i * 4);
		break;
	}
	case FFN_OP_BCM_WR:
		st = bcm_wr(a0, (u32)a1);
		break;
	case FFN_OP_SCHAN: {
		u64 ctrl = 0;

		st = schan_op(req + FFN_CPDP_HDR, (u32)a0,
			      rsp + FFN_CPDP_HDR, (u32)a1, &ctrl);
		r1 = ctrl;
		rlen = (u32)(a1 * 4);
		break;
	}
	case FFN_OP_LED_LOAD:
		st = led_load(a0 & 3, req + FFN_CPDP_HDR, len);
		r1 = len;
		break;
	case FFN_OP_LED_ENABLE: {
		u32 v = 0;

		st = led_enable(a0 & 3, (int)a1, &v);
		r1 = v;
		break;
	}
	case FFN_OP_LED_GET: {
		u64 unit = a0 & 3;
		u32 v = 0;

		st = bcm_rd(FFN_LEDUP0_CTRL + unit * FFN_LEDUP_STRIDE, &v);
		r1 = v;
		if (st == FFN_ST_OK) {
			st = bcm_rd(FFN_LEDUP0_CLK_DIV, &v);
			r2 = v;
		}
		break;
	}
	default:
		st = FFN_ST_BADOP;
		break;
	}

reply:
	wr32((volatile void *)(rsp + 0), seq);
	*(volatile u16 *)(rsp + 4) = op;
	*(volatile u16 *)(rsp + 6) = (u16)st;
	wr32((volatile void *)(rsp + 8), rlen);
	wr32((volatile void *)(rsp + 12),
	     rlen ? crc32((const u8 *)(rsp + FFN_CPDP_HDR), rlen) : 0);
	wr64((volatile void *)(rsp + 16), 0);
	wr64((volatile void *)(rsp + 24), r1);
	wr64((volatile void *)(rsp + 32), r2);
	barrier();
}

/*
 * The FE100 comes out of every OCTEON boot with its PCI memory decode OFF --
 * the kernel only ever logs "PCI: Enabling device" for domain 0003, never for
 * domain 0001 where the BCM88375 lives. Until this is set, every BAR read
 * returns 0xffffffff, which reads like a dead chip but is not. Owning the
 * transport means owning this, so the CP never has to remember it.
 */
static void enable_one(const char *path, const char *what)
{
	long fd = sys3(NR_open, path, 1 /*O_WRONLY*/, 0);

	if (fd < 0) {
		say("ffn_cpdpd: no enable node for ");
		say(what);
		say("\n");
		return;
	}
	if (sys3(NR_write, fd, "1", 1) == 1) {
		say("ffn_cpdpd: ");
		say(what);
		say(" PCI memory decode enabled\n");
	} else {
		say("ffn_cpdpd: ");
		say(what);
		say(" enable write failed\n");
	}
	sys1(NR_close, fd);
}

/*
 * Both chips need this, and an earlier version only did 0001 while calling it
 * "FE100" -- 0001 is the BCM88375, 0002 is the FE100. Getting that wrong meant
 * the FE100 was never enabled at all and read back as 0xffffffff.
 */
static void enable_devices(void)
{
	enable_one("/sys/bus/pci/devices/0001:01:00.0/enable",
		   "BCM88375 (Qumran)");
	enable_one("/sys/bus/pci/devices/0002:01:00.0/enable",
		   "FE100 (offload FPGA)");
}

static void nap_us(long us)
{
	struct {
		long sec;
		long nsec;
	} ts;

	ts.sec = us / 1000000;
	ts.nsec = (us % 1000000) * 1000;
	sys2(NR_nanosleep, &ts, 0);
}

void _start(void)
{
	volatile u8 *cp2dp, *dp2cp;
	volatile u8 *sup;
	u32 tail = 0, rhead = 0;
	u64 alive = 0;
	long r;

	memfd = (int)sys3(NR_open, "/dev/mem", O_RDWR, 0);
	if (memfd < 0) {
		say("ffn_cpdpd: cannot open /dev/mem\n");
		sys1(NR_exit_group, 1);
	}
	r = sys6(NR_mmap, 0, FFN_CPDP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
		 memfd, (long)FFN_CPDP_BASE);
	if (r < 0 && r > -4096) {
		say("ffn_cpdpd: cannot map the transport region\n");
		sys1(NR_exit_group, 1);
	}
	region = (volatile u8 *)r;
	sup = region;
	cp2dp = ring_of(FFN_CPDP_CP2DP_OFF);
	dp2cp = ring_of(FFN_CPDP_DP2CP_OFF);

	/* publish the superblock; magic last, so the CP never sees it half-built */
	wr32((volatile void *)(sup + 8), FFN_CPDP_VERSION);
	wr32((volatile void *)(sup + 12), FFN_CPDP_SLOT);
	wr32((volatile void *)(sup + 16), FFN_CPDP_NSLOTS);
	wr32((volatile void *)(sup + 20), 0);
	wr64((volatile void *)(sup + 24), FFN_CPDP_CP2DP_OFF);
	wr64((volatile void *)(sup + 32), FFN_CPDP_DP2CP_OFF);
	wr64((volatile void *)(sup + 40), 0);
	wr64((volatile void *)(sup + 48), 1);
	wr32((volatile void *)(cp2dp + 0), 0);
	wr32((volatile void *)(cp2dp + 4), 0);
	wr32((volatile void *)(dp2cp + 0), 0);
	wr32((volatile void *)(dp2cp + 4), 0);
	barrier();
	wr64((volatile void *)(sup + 0), FFN_CPDP_MAGIC);
	barrier();

	say("ffn_cpdpd: up, region 0x");
	sayhex(FFN_CPDP_BASE, 8);
	say(" magic published\n");

	enable_devices();

	for (;;) {
		u32 head;

		barrier();
		head = rd32(cp2dp + 0);
		if (head != tail) {
			volatile u8 *req = slot_of(cp2dp, tail);
			volatile u8 *rsp = slot_of(dp2cp, rhead);

			handle(req, rsp);
			rhead++;
			wr32((volatile void *)(dp2cp + 0), rhead);
			tail++;
			wr32((volatile void *)(cp2dp + 4), tail);
			barrier();
			continue;		/* drain without sleeping */
		}
		alive++;
		wr64((volatile void *)(sup + 40), alive);
		barrier();
		nap_us(2000);
	}
}
