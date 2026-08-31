/*
 * ffn_mem -- physical memory peek/poke for the Octeon, freestanding MIPS64.
 *
 * The vendor busybox on this box has no devmem applet (and no shell), and the
 * SDK toolchain has no libc.a, so this is FFN's own: direct syscalls, no libc,
 * depends on nothing. Needed to reach two things that have no driver yet:
 *
 *   - the CPLD LED registers (chassis status lights)
 *   - the FE100 / BCM88375 BARs, at 0x11c0101800000 (32K) and
 *     0x11c0100800000 (8M) as the Octeon's own PCI enumeration reports them
 *
 * Commands come from stdin, one per line, so no argv handling is needed:
 *
 *   r8|r16|r32|r64 <addr> [count]
 *   w8|w16|w32|w64 <addr> <value>
 *   q
 *
 * CAUTION: reading an address that decodes to nothing raises a bus error. On
 * the host that hung a core; here it kills this process, which is survivable --
 * but sweep unknown windows in small steps.
 */

#define NR_read		5000
#define NR_write	5001
#define NR_open		5002
#define NR_close	5003
#define NR_mmap		5009
#define NR_munmap	5011
#define NR_exit_group	5205

#define O_RDWR		2
#define PROT_READ	1
#define PROT_WRITE	2
#define MAP_SHARED	1

#define WIN		0x10000UL		/* map a 64K window per access */

typedef unsigned long long u64;
typedef unsigned int u32;
typedef unsigned short u16;
typedef unsigned char u8;
typedef long s64;

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
#define sys3(n, a, b, c)	sys6(n, (long)(a), (long)(b), (long)(c), 0, 0, 0)

static u64 slen(const char *s)
{
	u64 n = 0;

	while (s[n])
		n++;
	return n;
}

static void out(const char *s)
{
	sys3(NR_write, 1, s, slen(s));
}

static void outhex(u64 v, int digits)
{
	char b[20];
	int i;

	for (i = 0; i < digits; i++)
		b[digits - 1 - i] = "0123456789abcdef"[(v >> (i * 4)) & 0xf];
	b[digits] = 0;
	out(b);
}

/* parse hex (0x...) or decimal; advances *p past the number */
static u64 num(const char **p)
{
	const char *s = *p;
	u64 v = 0;

	while (*s == 32 || *s == 9)
		s++;
	if (s[0] == 48 && (s[1] == 120 || s[1] == 88)) {
		s += 2;
		for (;;) {
			int c = *s, d;

			if (c >= 48 && c <= 57)
				d = c - 48;
			else if (c >= 97 && c <= 102)
				d = c - 87;
			else if (c >= 65 && c <= 70)
				d = c - 55;
			else
				break;
			v = (v << 4) | (u64)d;
			s++;
		}
	} else {
		while (*s >= 48 && *s <= 57) {
			v = v * 10 + (u64)(*s - 48);
			s++;
		}
	}
	*p = s;
	return v;
}

static int memfd = -1;

/* map the 64K window containing addr; returns the mapped base or 0 */
static volatile void *win(u64 addr, u64 *off)
{
	u64 base = addr & ~(WIN - 1);
	long r = sys6(NR_mmap, 0, WIN, PROT_READ | PROT_WRITE, MAP_SHARED,
		      memfd, (long)base);

	if (r < 0 && r > -4096) {
		out("  mmap failed, errno ");
		outhex((u64)(-r), 4);
		out("\n");
		return 0;
	}
	*off = addr - base;
	return (volatile void *)r;
}

static void unwin(volatile void *p)
{
	sys6(NR_munmap, (long)p, WIN, 0, 0, 0, 0);
}

static void do_read(u64 addr, u64 count, int width)
{
	u64 i;

	for (i = 0; i < count; i++) {
		u64 a = addr + i * (u64)(width / 8);
		u64 off, v;
		volatile void *m = win(a, &off);

		if (!m)
			return;
		if (width == 8)
			v = *(volatile u8 *)((char *)m + off);
		else if (width == 16)
			v = *(volatile u16 *)((char *)m + off);
		else if (width == 32)
			v = *(volatile u32 *)((char *)m + off);
		else
			v = *(volatile u64 *)((char *)m + off);
		unwin(m);

		out("  0x");
		outhex(a, 13);
		out(" = 0x");
		outhex(v, width / 4);
		out("\n");
	}
}

static void do_write(u64 addr, u64 val, int width)
{
	u64 off;
	volatile void *m = win(addr, &off);

	if (!m)
		return;
	if (width == 8)
		*(volatile u8 *)((char *)m + off) = (u8)val;
	else if (width == 16)
		*(volatile u16 *)((char *)m + off) = (u16)val;
	else if (width == 32)
		*(volatile u32 *)((char *)m + off) = (u32)val;
	else
		*(volatile u64 *)((char *)m + off) = val;
	unwin(m);
	out("  wrote 0x");
	outhex(val, width / 4);
	out(" -> 0x");
	outhex(addr, 13);
	out("\n");
}

static int eqn(const char *a, const char *b, int n)
{
	int i;

	for (i = 0; i < n; i++)
		if (a[i] != b[i])
			return 0;
	return 1;
}

void _start(void)
{
	char line[256];
	long n;

	memfd = (int)sys3(NR_open, "/dev/mem", O_RDWR, 0);
	if (memfd < 0) {
		out("ffn_mem: cannot open /dev/mem\n");
		sys1(NR_exit_group, 1);
	}
	out("ffn_mem ready. commands: r8|r16|r32|r64 <addr> [count] | "
	    "w8|w16|w32|w64 <addr> <val> | q\n");

	for (;;) {
		u64 i = 0;

		/* read one line */
		for (;;) {
			char c;

			n = sys3(NR_read, 0, &c, 1);
			if (n <= 0) {
				sys1(NR_exit_group, 0);
			}
			if (c == 10 || c == 13)
				break;
			if (i < sizeof(line) - 1)
				line[i++] = c;
		}
		line[i] = 0;

		{
			const char *p = line;
			int width = 0, wr = 0;

			while (*p == 32 || *p == 9)
				p++;
			if (*p == 113) {              /* q */
				out("bye\n");
				sys1(NR_exit_group, 0);
			}
			if (*p == 0)
				continue;
			if (*p == 114)                /* r */
				wr = 0;
			else if (*p == 119)           /* w */
				wr = 1;
			else {
				out("  ? unknown command\n");
				continue;
			}
			p++;
			if (eqn(p, "64", 2)) {
				width = 64;
				p += 2;
			} else if (eqn(p, "32", 2)) {
				width = 32;
				p += 2;
			} else if (eqn(p, "16", 2)) {
				width = 16;
				p += 2;
			} else if (*p == 56) {        /* 8 */
				width = 8;
				p += 1;
			} else {
				out("  ? width must be 8, 16, 32 or 64\n");
				continue;
			}
			{
				u64 addr = num(&p);
				u64 arg;

				while (*p == 32 || *p == 9)
					p++;
				arg = *p ? num(&p) : 0;
				if (wr)
					do_write(addr, arg, width);
				else
					do_read(addr, arg ? arg : 1, width);
			}
		}
	}
}
