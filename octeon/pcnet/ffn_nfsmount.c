/*
 * ffn_nfsmount -- a minimal NFS mount helper for the OCTEON.
 *
 * The lean initramfs has no mount.nfs (nfs-utils), and busybox dropped its
 * built-in NFS client, so `mount -t nfs` fails with ENOENT (no helper). But the
 * kernel's text-based NFS mount API does the whole job -- including the mountd
 * RPC for v3 -- from an options string. So this is all a helper needs to be:
 * pass "hostname:/export", a target, and an options string to mount(2), and the
 * in-kernel NFS client contacts the server itself.
 *
 *   ffn_nfsmount 127.1.1.1:/opt/dpfs /mnt/dpfs "vers=3,addr=127.1.1.1,..."
 *
 * Built raw-syscall and static (no libc) so it is a few KB and drops into the
 * initramfs -- and small enough to shove across the serial console when
 * iterating, unlike a 700 KB glibc-static binary.
 */

typedef unsigned long u64;

/* MIPS n64 syscall: number in $v0 (base 5000), args $a0..; $a3!=0 => error. */
static long msys(long n, u64 a, u64 b, u64 c, u64 d, u64 e)
{
	register u64 v0 __asm__("$2") = (u64)n;
	register u64 a0 __asm__("$4") = a;
	register u64 a1 __asm__("$5") = b;
	register u64 a2 __asm__("$6") = c;
	register u64 a3 __asm__("$7") = d;
	register u64 a4 __asm__("$8") = e;

	__asm__ __volatile__(
		"syscall\n"
		: "+r"(v0), "+r"(a3)
		: "r"(a0), "r"(a1), "r"(a2), "r"(a3), "r"(a4)
		: "$1", "$3", "$9", "$10", "$11", "$12", "$13",
		  "$14", "$15", "$24", "$25", "memory");
	return a3 ? -(long)v0 : (long)v0;
}

#define NR_mount	5160
#define NR_write	5001
#define NR_exit		5058

static u64 slen(const char *s)
{
	u64 n = 0;
	while (s[n])
		n++;
	return n;
}

static void say(const char *s)
{
	msys(NR_write, 2, (u64)s, slen(s), 0, 0);
}

/*
 * Freestanding entry. The kernel hands us argc at 0(sp) and argv following, but
 * a C _start would let gcc emit a prologue that moves $sp before we could read
 * it -- which segfaulted the first version. So _start is a pure asm stub that
 * captures the entry $sp into $a0 and tail-calls ffn_main before any frame is
 * set up; ffn_main then receives the original stack pointer as its argument.
 */
__asm__(
	".text\n"
	".globl _start\n"
	".ent _start\n"
	"_start:\n"
	"	move $4, $29\n"   /* a0 = entry sp */
	"	and  $29, $29, -16\n" /* 16-byte align the stack for the call */
	"	jal  ffn_main\n"
	"	nop\n"
	".end _start\n"
);

void ffn_main(u64 *sp)
{
	long argc;
	char **argv;
	long rc;

	argc = (long)sp[0];
	argv = (char **)&sp[1];

	if (argc < 4) {
		say("usage: ffn_nfsmount host:/export target options\n");
		msys(NR_exit, 2, 0, 0, 0, 0);
	}

	rc = msys(NR_mount, (u64)argv[1], (u64)argv[2],
		  (u64)"nfs", 0, (u64)argv[3]);
	if (rc < 0) {
		char buf[32];
		long e = -rc, i = 0, j;
		say("ffn_nfsmount: mount failed, errno ");
		if (e == 0)
			buf[i++] = '0';
		while (e && i < 20) {
			buf[i++] = '0' + (e % 10);
			e /= 10;
		}
		for (j = i - 1; j >= 0; j--) {
			char c = buf[j];
			msys(NR_write, 2, (u64)&c, 1, 0, 0);
		}
		say("\n");
		msys(NR_exit, 1, 0, 0, 0, 0);
	}
	say("ffn_nfsmount: mounted\n");
	msys(NR_exit, 0, 0, 0, 0, 0);
}
