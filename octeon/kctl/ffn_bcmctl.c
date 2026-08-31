/*
 * ffn_bcmctl -- exercise FFN's OCTEON III BCM control driver from the Octeon.
 *
 * Freestanding MIPS64 N64, no libc, same reason as ffn_init.c: the SDK
 * toolchain ships no libc.a, and the initramfs FFN boots has no vendor
 * userland in it. Direct syscalls mean this binary depends on nothing, so it
 * can live in FFN's own initramfs and ship with it.
 *
 * The ioctl numbers are NOT written out by hand here. This file includes the
 * driver's own ffn_bcm_abi.h and is compiled against the kernel's uapi headers,
 * so _IOWR() expands exactly as it did when the driver was built. That matters
 * more than usual on MIPS: MIPS does not use the generic ioctl encoding. It has
 * _IOC_NONE=1/_IOC_READ=2/_IOC_WRITE=4 in a 3-bit direction field and 13 size
 * bits, where the generic encoding has NONE=0/WRITE=1/READ=2 in 2 bits and 14
 * size bits -- so _IOR and _IOW come out with *different* values on MIPS than
 * on x86. Hand-computing them is a good way to get -ENOTTY and a long
 * afternoon.
 *
 * Commands are read from stdin, one per line, so no argv handling is needed
 * (the same choice ffn_mem.c made, and it works whether a shell is present or
 * FFN's init pipes a script in):
 *
 *   info                          driver + device identity, S-channel counters
 *   rd <bar> <off> [count]        read register(s); bar is 0 or 2
 *   wr <bar> <off> <val>          write one register
 *   schan <nrecv> <w0> [w1 ...]   one S-channel transaction
 *   ledstat <unit>                LEDUP CTRL / CLK_DIV / first RAM bytes
 *   leden <unit> <0|1>            clear/set LEDUP_EN
 *   ledload <unit> <path>         load a LED program into PROGRAM_RAM
 *   quit
 */

#include "ffn_bcm_abi.h"

#define NR_read		5000
#define NR_write	5001
#define NR_open		5002
#define NR_close	5003
#define NR_ioctl	5015
#define NR_exit_group	5205

#define O_RDONLY	0
#define O_RDWR		2

typedef unsigned long	u64;
typedef long		s64;
typedef unsigned int	u32;
typedef unsigned char	u8;

/* Same wrapper as ffn_init.c, unchanged: r7 (a3) is the MIPS error flag, and
 * when it is set r2 holds the errno rather than a return value. */
static long sys5(long n, long a, long b, long c, long d, long e)
{
	register long r2 __asm__("$2");
	register long r4 __asm__("$4") = a;
	register long r5 __asm__("$5") = b;
	register long r6 __asm__("$6") = c;
	register long r7 __asm__("$7") = d;
	register long r8 __asm__("$8") = e;
	__asm__ __volatile__(
		"daddu $2,$0,%2 ; syscall"
		: "=&r"(r2), "+r"(r7)
		: "ir"(n), "r"(r4), "r"(r5), "r"(r6), "r"(r8)
		: "$1", "$3", "$9", "$10", "$11", "$12", "$13", "$14", "$15",
		  "$24", "$25", "hi", "lo", "memory");
	return r7 ? -r2 : r2;
}

#define sys1(n, a)		sys5(n, (long)(a), 0, 0, 0, 0)
#define sys3(n, a, b, c)	sys5(n, (long)(a), (long)(b), (long)(c), 0, 0)

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

static void outd(s64 v)
{
	char b[24];
	int i = 23;
	int neg = v < 0;

	if (neg)
		v = -v;
	b[i--] = 0;
	if (!v)
		b[i--] = 48;
	while (v) {
		b[i--] = 48 + (int)(v % 10);
		v /= 10;
	}
	if (neg)
		b[i--] = 45;
	out(&b[i + 1]);
}

/* fixed-width hex, so columns of register values line up */
static void outh(u64 v, int digits)
{
	static const char hx[] = "0123456789abcdef";
	char b[19];
	int i;

	b[0] = 48;
	b[1] = 120;			/* "0x" */
	for (i = 0; i < digits; i++)
		b[2 + i] = hx[(v >> ((digits - 1 - i) * 4)) & 0xf];
	b[2 + digits] = 0;
	out(b);
}

static void zero(void *p, u64 n)
{
	u8 *b = p;

	while (n--)
		*b++ = 0;
}

/* ---- line reading and tokenising ------------------------------------------ */

/* Read one line from stdin. Returns length, or -1 at EOF. Reads a byte at a
 * time: this is a control tool issuing a handful of commands, and byte reads
 * keep it from consuming input a following program might want. */
static int readline(char *buf, int cap)
{
	int n = 0;
	char c;

	for (;;) {
		long r = sys3(NR_read, 0, &c, 1);

		if (r <= 0)
			return n ? n : -1;
		if (c == 10)
			break;
		if (n < cap - 1)
			buf[n++] = c;
	}
	buf[n] = 0;
	return n;
}

static char *tok(char **p)
{
	char *s = *p;
	char *start;

	while (*s == 32 || *s == 9)
		s++;
	if (!*s) {
		*p = s;
		return 0;
	}
	start = s;
	while (*s && *s != 32 && *s != 9)
		s++;
	if (*s)
		*s++ = 0;
	*p = s;
	return start;
}

/* Accepts 0x-prefixed hex or plain decimal. Returns 0 on a bad token so a
 * typo reads as zero rather than as whatever was left in the variable; the
 * caller prints what it parsed, so a mistake is visible. */
static u64 num(const char *s)
{
	u64 v = 0;

	if (!s)
		return 0;
	if (s[0] == 48 && (s[1] == 120 || s[1] == 88)) {
		s += 2;
		while (*s) {
			u64 d;

			if (*s >= 48 && *s <= 57)
				d = *s - 48;
			else if (*s >= 97 && *s <= 102)
				d = *s - 97 + 10;
			else if (*s >= 65 && *s <= 70)
				d = *s - 65 + 10;
			else
				break;
			v = v * 16 + d;
			s++;
		}
		return v;
	}
	while (*s >= 48 && *s <= 57) {
		v = v * 10 + (*s - 48);
		s++;
	}
	return v;
}

static int streq(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++;
		b++;
	}
	return *a == *b;
}

/* ---- driver access -------------------------------------------------------- */

static long fd = -1;

/* Report an ioctl failure with the errno, since -ENODEV (driver not bound),
 * -ENOTTY (wrong ioctl encoding) and -ETIMEDOUT (S-channel never completed)
 * are three completely different problems. */
static void fail(const char *what, long rc)
{
	out("  ");
	out(what);
	out(" failed, errno ");
	outd(-rc);
	out("\n");
}

static void cmd_info(void)
{
	struct ffn_bcm_info in;
	long rc;

	zero(&in, sizeof(in));
	rc = sys3(NR_ioctl, fd, FFN_BCM_IOC_INFO, &in);
	if (rc < 0) {
		fail("info", rc);
		return;
	}
	out("  pci        ");
	out(in.pci);
	out("\n  device     ");
	outh(in.vendor, 4);
	out(":");
	outh(in.device, 4);
	out(" rev ");
	outh(in.revision, 2);
	out("\n  ident      ");
	outh(in.ident, 8);
	out("   (low half should be ");
	outh(in.device, 4);
	out(")\n  bar0       ");
	outh(in.bar0_phys, 16);
	out("  ");
	outd(in.bar0_len);
	out(" bytes\n  bar2 cmic  ");
	outh(in.bar2_phys, 16);
	out("  ");
	outd(in.bar2_len);
	out(" bytes\n  schan      ");
	outd(in.schan_ops);
	out(" ops, ");
	outd(in.schan_errs);
	out(" errs, ");
	outd(in.schan_timeouts);
	out(" timeouts\n");
}

static void cmd_rd(u32 bar, u32 off, u32 count)
{
	struct ffn_bcm_reg r;
	long rc;
	u32 i;

	zero(&r, sizeof(r));
	r.bar = bar;
	r.off = off;
	r.count = count ? count : 1;
	rc = sys3(NR_ioctl, fd, FFN_BCM_IOC_RD, &r);
	if (rc < 0) {
		fail("rd", rc);
		return;
	}
	for (i = 0; i < r.count; i++) {
		out("  bar");
		outd(bar);
		out("+");
		outh(off + i * 4, 5);
		out(" = ");
		outh(r.vals[i], 8);
		out("\n");
	}
}

static void cmd_wr(u32 bar, u32 off, u32 val)
{
	struct ffn_bcm_reg r;
	long rc;

	zero(&r, sizeof(r));
	r.bar = bar;
	r.off = off;
	r.val = val;
	rc = sys3(NR_ioctl, fd, FFN_BCM_IOC_WR, &r);
	if (rc < 0) {
		fail("wr", rc);
		return;
	}
	out("  wrote ");
	outh(val, 8);
	out(" -> bar");
	outd(bar);
	out("+");
	outh(off, 5);
	out("\n");
}

/* Decode SCHAN_CTRL so a failure says which bit came back, not just "error". */
static void print_ctrl(u32 ctrl)
{
	static const char *names[] = {
		"MSG_START", "MSG_DONE", "ABORT",
		"SER_CHECK_FAIL", "NACK", "TIMEOUT", "SCHAN_ERROR"
	};
	static const int bits[] = { 0, 1, 2, 20, 21, 22, 23 };
	int i, first = 1;

	out("  SCHAN_CTRL = ");
	outh(ctrl, 8);
	out("  (");
	for (i = 0; i < 7; i++) {
		if (!(ctrl & (1u << bits[i])))
			continue;
		if (!first)
			out(", ");
		out(names[i]);
		first = 0;
	}
	if (first)
		out("idle");
	out(")\n");
}

static void cmd_schan(struct ffn_bcm_schan *s)
{
	long rc;
	u32 i;

	rc = sys3(NR_ioctl, fd, FFN_BCM_IOC_SCHAN, s);
	/* ctrl, spins and pre_err are filled in even on failure -- the driver
	 * copies the struct back before returning the error, because which
	 * error bit came back is the whole diagnostic. */
	print_ctrl(s->ctrl);
	out("  polls      ");
	outd(s->spins);
	out("\n");
	/* Error bits on this chip latch and never clear, so ctrl alone cannot
	 * say whether the failure belongs to THIS message. Show what was
	 * already standing before it started. */
	if (s->pre_err) {
		out("  already latched before this message: ");
		outh(s->pre_err, 8);
		out("  (not this message's failure)\n");
	}
	if (rc < 0) {
		fail("schan", rc);
		return;
	}
	for (i = 0; i < s->nrecv; i++) {
		out("  MESSAGE");
		outd(i);
		out("   = ");
		outh(s->msg[i], 8);
		out("\n");
	}
}

static void cmd_ledstat(u32 unit)
{
	/* CMIC_LEDUP<unit> offsets, mirroring the driver's own constants. */
	u32 base = 0x20000 + unit * 0x1000;

	out("  LEDUP");
	outd(unit);
	out(" CTRL / CLK_PARAMS / CLK_DIV:\n");
	cmd_rd(FFN_BCM_BAR_CMIC, base, 1);
	cmd_rd(FFN_BCM_BAR_CMIC, base + 0x50, 1);
	cmd_rd(FFN_BCM_BAR_CMIC, base + 0x5c, 1);
	out("  PROGRAM_RAM[0:8]:\n");
	cmd_rd(FFN_BCM_BAR_CMIC, base + 0x800, 8);
	out("  DATA_RAM[0:8]:\n");
	cmd_rd(FFN_BCM_BAR_CMIC, base + 0x400, 8);
}

static void cmd_leden(u32 unit, u32 on)
{
	struct ffn_bcm_led l;
	long rc;

	zero(&l, sizeof(l));
	l.unit = unit;
	l.enable = on;
	rc = sys3(NR_ioctl, fd, FFN_BCM_IOC_LED_EN, &l);
	if (rc < 0) {
		fail("leden", rc);
		return;
	}
	out("  LEDUP");
	outd(unit);
	out(" CTRL = ");
	outh(l.ctrl, 8);
	out("  LEDUP_EN=");
	out((l.ctrl & 1) ? "on" : "off");
	out("\n");
	if (on && !(l.ctrl & 1))
		out("  note: the enable bit did not stick -- the LED processor "
		    "is gated elsewhere\n");
}

static void cmd_ledload(u32 unit, const char *path)
{
	struct ffn_bcm_led l;
	long pf, n, rc;

	zero(&l, sizeof(l));
	l.unit = unit;

	pf = sys3(NR_open, path, O_RDONLY, 0);
	if (pf < 0) {
		out("  cannot open ");
		out(path);
		out("\n");
		return;
	}
	n = sys3(NR_read, pf, l.prog, FFN_BCM_LED_RAMSZ);
	sys1(NR_close, pf);
	if (n <= 0) {
		out("  empty program\n");
		return;
	}
	l.len = (u32)n;

	rc = sys3(NR_ioctl, fd, FFN_BCM_IOC_LED_LOAD, &l);
	if (rc < 0) {
		fail("ledload", rc);
		return;
	}
	out("  loaded ");
	outd(l.len);
	out(" program bytes into LEDUP");
	outd(unit);
	out(" PROGRAM_RAM, CTRL = ");
	outh(l.ctrl, 8);
	out("\n");
	/* Read the first bytes back, so a write that silently did nothing
	 * cannot pass for success. */
	out("  readback:\n");
	cmd_rd(FFN_BCM_BAR_CMIC, 0x20800 + unit * 0x1000,
	       l.len < 8 ? l.len : 8);
}

static void usage(void)
{
	out("commands:\n"
	    "  info\n"
	    "  rd <bar> <off> [count]\n"
	    "  wr <bar> <off> <val>\n"
	    "  schan <nrecv> <w0> [w1 ...]\n"
	    "  ledstat <unit>\n"
	    "  leden <unit> <0|1>\n"
	    "  ledload <unit> <path>\n"
	    "  quit\n");
}

void _start(void)
{
	char line[512];

	fd = sys3(NR_open, "/dev/" FFN_BCM_DEVNAME, O_RDWR, 0);
	if (fd < 0) {
		out("ffn_bcmctl: cannot open /dev/" FFN_BCM_DEVNAME
		    ", errno ");
		outd(-fd);
		out("\n  is ffn_bcm loaded?  modprobe ffn_bcm\n");
		sys1(NR_exit_group, 1);
	}
	out("ffn_bcmctl: /dev/" FFN_BCM_DEVNAME " open\n");

	for (;;) {
		char *p = line;
		char *c;

		if (readline(line, sizeof(line)) < 0)
			break;
		c = tok(&p);
		if (!c)
			continue;
		if (c[0] == 35)			/* '#' comment */
			continue;

		if (streq(c, "info")) {
			cmd_info();
		} else if (streq(c, "rd")) {
			u32 bar = (u32)num(tok(&p));
			u32 off = (u32)num(tok(&p));

			cmd_rd(bar, off, (u32)num(tok(&p)));
		} else if (streq(c, "wr")) {
			u32 bar = (u32)num(tok(&p));
			u32 off = (u32)num(tok(&p));

			cmd_wr(bar, off, (u32)num(tok(&p)));
		} else if (streq(c, "schan")) {
			struct ffn_bcm_schan s;
			u32 n = 0;

			zero(&s, sizeof(s));
			s.nrecv = (u32)num(tok(&p));
			while (n < FFN_BCM_SCHAN_NMSG) {
				char *w = tok(&p);

				if (!w)
					break;
				s.msg[n++] = (u32)num(w);
			}
			s.nsend = n;
			if (!n)
				out("  schan needs at least one word\n");
			else
				cmd_schan(&s);
		} else if (streq(c, "ledstat")) {
			cmd_ledstat((u32)num(tok(&p)));
		} else if (streq(c, "leden")) {
			u32 unit = (u32)num(tok(&p));

			cmd_leden(unit, (u32)num(tok(&p)));
		} else if (streq(c, "ledload")) {
			u32 unit = (u32)num(tok(&p));
			char *path = tok(&p);

			if (!path)
				out("  ledload needs a path\n");
			else
				cmd_ledload(unit, path);
		} else if (streq(c, "quit") || streq(c, "exit")) {
			break;
		} else {
			out("  unknown command: ");
			out(c);
			out("\n");
			usage();
		}
	}

	sys1(NR_close, fd);
	sys1(NR_exit_group, 0);
}
