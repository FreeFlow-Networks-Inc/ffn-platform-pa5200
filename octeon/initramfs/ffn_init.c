/*
 * FFN Octeon initramfs init -- freestanding, MIPS64 N64, no libc.
 *
 * Why no libc: the vendor busybox in this initramfs has NO shell applet at all
 * (no sh, no ash, no hush), so a "#!/bin/sh" init could never run -- the kernel
 * exec'd it, busybox printed "sh: applet not found" and exited 1, which the
 * kernel reported as "Attempted to kill init! exitcode=0x100". And the SDK
 * toolchain ships no libc.a, so there is nothing to statically link against
 * either. Direct syscalls sidestep both: this binary depends on nothing, which
 * also means FFN can ship it -- no vendor userland in the critical path.
 *
 * It mounts the pseudo filesystems, reports what the kernel found, brings the
 * BGX ports up, and then stays alive forever. Staying alive matters: an init
 * that returns is a panic.
 */

#define NR_read		5000
#define NR_write	5001
#define NR_open		5002
#define NR_close	5003
#define NR_ioctl	5015
#define NR_nanosleep	5034
#define NR_socket	5040
#define NR_uname	5061
#define NR_mount	5160
#define NR_getdents64	5308
#define NR_dup2		5032
#define NR_fork		5056
#define NR_execve	5057
#define NR_wait4	5059
#define NR_exit_group	5205

#define O_RDONLY	0
#define O_RDWR		2

#define SIOCGIFFLAGS	0x8913
#define SIOCSIFFLAGS	0x8914
#define IFF_UP		0x1

typedef unsigned long	u64;
typedef long		s64;
typedef unsigned short	u16;
typedef unsigned char	u8;

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
#define sys2(n, a, b)		sys5(n, (long)(a), (long)(b), 0, 0, 0)
#define sys3(n, a, b, c)	sys5(n, (long)(a), (long)(b), (long)(c), 0, 0)

static int cfd = 1;			/* console: fd 1 unless we open one */

static u64 slen(const char *s)
{
	u64 n = 0;

	while (s[n])
		n++;
	return n;
}

static void out(const char *s)
{
	sys3(NR_write, cfd, s, slen(s));
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
		b[i--] = 48 + (v % 10);
		v /= 10;
	}
	if (neg)
		b[i--] = 45;
	out(&b[i + 1]);
}

static long slurp(const char *path, char *buf, u64 cap)
{
	long fd = sys3(NR_open, path, O_RDONLY, 0);
	long n;

	if (fd < 0)
		return -1;
	n = sys3(NR_read, fd, buf, cap - 1);
	sys1(NR_close, fd);
	if (n < 0)
		n = 0;
	while (n > 0 && (buf[n - 1] == 10 || buf[n - 1] == 13))
		n--;
	buf[n] = 0;
	return n;
}

static u64 joinpath(char *dst, const char *dir, const char *name)
{
	u64 i = 0, j;

	for (j = 0; dir[j]; j++)
		dst[i++] = dir[j];
	dst[i++] = 47;
	for (j = 0; name[j]; j++)
		dst[i++] = name[j];
	dst[i] = 0;
	return i;
}

static void catfile(const char *dir, const char *name, const char *label)
{
	char path[192];
	char val[128];

	joinpath(path, dir, name);
	out(label);
	if (slurp(path, val, sizeof(val)) >= 0)
		out(val);
	else
		out("?");
}

struct dirent64 {
	u64 d_ino;
	s64 d_off;
	u16 d_reclen;
	u8 d_type;
	char d_name[1];
};

static void walk(const char *dir, void (*fn)(const char *dir, const char *name))
{
	char buf[8192];
	long fd = sys3(NR_open, dir, O_RDONLY, 0);
	long n;

	if (fd < 0) {
		out("  (cannot open ");
		out(dir);
		out(")\n");
		return;
	}
	while ((n = sys3(NR_getdents64, fd, buf, sizeof(buf))) > 0) {
		long off = 0;

		while (off < n) {
			struct dirent64 *d = (struct dirent64 *)(buf + off);

			if (d->d_name[0] != 46)
				fn(dir, d->d_name);
			off += d->d_reclen;
		}
	}
	sys1(NR_close, fd);
}

static void show_pci(const char *dir, const char *name)
{
	char sub[192];

	joinpath(sub, dir, name);
	out("  ");
	out(name);
	catfile(sub, "vendor", "  vendor=");
	catfile(sub, "device", " device=");
	catfile(sub, "class", " class=");
	out("\n");
}

static void show_net(const char *dir, const char *name)
{
	char sub[192];

	joinpath(sub, dir, name);
	out("  ");
	out(name);
	catfile(sub, "address", "  mac=");
	catfile(sub, "operstate", " operstate=");
	catfile(sub, "carrier", " carrier=");
	out("\n");
}

struct ifreq_flags {
	char ifr_name[16];
	short ifr_flags;
	char pad[22];
};

static void link_up(const char *ifname)
{
	struct ifreq_flags r;
	long s = sys3(NR_socket, 2, 2, 0);
	u64 i;

	if (s < 0) {
		out("    socket() failed: ");
		outd(s);
		out("\n");
		return;
	}
	for (i = 0; i < sizeof(r); i++)
		((char *)&r)[i] = 0;
	for (i = 0; ifname[i] && i < 15; i++)
		r.ifr_name[i] = ifname[i];

	if (sys3(NR_ioctl, s, SIOCGIFFLAGS, &r) < 0) {
		out("    ");
		out(ifname);
		out(": SIOCGIFFLAGS failed (no such interface?)\n");
		sys1(NR_close, s);
		return;
	}
	r.ifr_flags |= IFF_UP;
	if (sys3(NR_ioctl, s, SIOCSIFFLAGS, &r) < 0) {
		out("    ");
		out(ifname);
		out(": SIOCSIFFLAGS failed\n");
	} else {
		out("    ");
		out(ifname);
		out(": IFF_UP set\n");
	}
	sys1(NR_close, s);
}

static void nap(long secs)
{
	struct {
		s64 sec;
		s64 nsec;
	} ts;

	ts.sec = secs;
	ts.nsec = 0;
	sys2(NR_nanosleep, &ts, 0);
}

struct utsname {
	char sysname[65], nodename[65], release[65];
	char version[65], machine[65], domainname[65];
};

static void show_cpus(void)
{
	char v[128];

	out("--- CPU cores ---\n");
	if (slurp("/sys/devices/system/cpu/present", v, sizeof(v)) >= 0) {
		out("  present : ");
		out(v);
		out("\n");
	}
	if (slurp("/sys/devices/system/cpu/online", v, sizeof(v)) >= 0) {
		out("  online  : ");
		out(v);
		out("\n");
	}
	if (slurp("/sys/devices/system/cpu/possible", v, sizeof(v)) >= 0) {
		out("  possible: ");
		out(v);
		out("\n");
	}
	out("\n");
}

static int have(const char *path)
{
	long fd = sys3(NR_open, path, O_RDONLY, 0);

	if (fd < 0)
		return 0;
	sys1(NR_close, fd);
	return 1;
}

/*
 * Run the overlay shell on the console. The child gets the console on fds
 * 0/1/2 explicitly -- bash misbehaves if it inherits something else.
 */
static void run_shell(const char *path)
{
	static char *argv[2];
	static char *envp[5];
	long pid;

	argv[0] = (char *)path;
	argv[1] = 0;
	envp[0] = (char *)"PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin";
	envp[1] = (char *)"TERM=vt100";
	envp[2] = (char *)"HOME=/";
	envp[3] = (char *)"PS1=FFN-oct# ";
	envp[4] = 0;

	pid = sys5(NR_fork, 0, 0, 0, 0, 0);
	if (pid == 0) {
		sys2(NR_dup2, cfd, 0);
		sys2(NR_dup2, cfd, 1);
		sys2(NR_dup2, cfd, 2);
		sys3(NR_execve, path, argv, envp);
		out("    execve failed\n");
		sys1(NR_exit_group, 1);
	}
	if (pid < 0) {
		out("    fork failed\n");
		return;
	}
	sys5(NR_wait4, pid, 0, 0, 0, 0);
}

void _start(void)
{
	struct utsname u;
	long fd;
	int round;

	sys5(NR_mount, (long)"proc", (long)"/proc", (long)"proc", 0, 0);
	sys5(NR_mount, (long)"sysfs", (long)"/sys", (long)"sysfs", 0, 0);
	sys5(NR_mount, (long)"dev", (long)"/dev", (long)"devtmpfs", 0, 0);

	fd = sys3(NR_open, "/dev/console", O_RDWR, 0);
	if (fd >= 0)
		cfd = (int)fd;

	out("\n");
	out("############################################\n");
	out("###  FFN Octeon initramfs -- it booted   ###\n");
	out("############################################\n\n");

	if (sys1(NR_uname, &u) == 0) {
		out("--- uname ---\n  ");
		out(u.sysname);
		out(" ");
		out(u.release);
		out(" ");
		out(u.machine);
		out("\n\n");
	}

	show_cpus();

	out("--- PCI devices behind the Octeon root complexes ---\n");
	walk("/sys/bus/pci/devices", show_pci);
	out("\n");

	out("--- network interfaces ---\n");
	walk("/sys/class/net", show_net);
	out("\n");

	out("--- bringing the BGX2 NIF ports up ---\n");
	link_up("eth0");
	link_up("eth1");
	nap(3);
	out("\n--- link state after IFF_UP ---\n");
	walk("/sys/class/net", show_net);
	out("\n");

	/*
	 * If the overlay carries the NFS-root flow, run it: bring up pcnet,
	 * NFS-mount the MP's full rootfs, and chroot into it (FFN's unified-
	 * storage model -- the OCTEON runs a full userland served from the MP
	 * over PCIe rather than one baked into this initramfs).
	 *
	 * ffn-nfsroot is written to be safe to auto-run: it drops to a plain
	 * console on any failure and re-enters an existing mount, so this loop
	 * both survives a not-yet-up transport and re-runs it when the chrooted
	 * shell exits. That is why it is preferred over the bare shell below.
	 */
	if (have("/sbin/ffn-nfsroot")) {
		out("FFN> NFS-root: /sbin/ffn-nfsroot "
		    "(pcnet -> mount MP rootfs -> chroot)\n\n");
		for (;;) {
			run_shell("/sbin/ffn-nfsroot");
			out("\nFFN> ffn-nfsroot returned; restarting it\n");
			nap(2);
		}
	}

	/*
	 * If the ffn_rootfs overlay landed it brought a real shell; give the
	 * operator a prompt. Drive it through the console broker:
	 *   echo 'ls /' > /run/ffn-octeon-console.in
	 */
	if (have("/bin/bash") || have("/bin/sh")) {
		const char *sh = have("/bin/bash") ? "/bin/bash" : "/bin/sh";

		out("FFN> overlay rootfs present -- starting ");
		out(sh);
		out("\n");
		out("FFN> send commands with: "
		    "echo 'cmd' > /run/ffn-octeon-console.in\n\n");
		for (;;) {
			run_shell(sh);
			out("\nFFN> shell exited, restarting it\n");
			nap(1);
		}
	}

	out("FFN> no overlay; link state every 30s. Nothing here exits.\n\n");

	for (round = 1;; round++) {
		nap(30);
		out("--- link poll ");
		outd(round);
		out(" ---\n");
		walk("/sys/class/net", show_net);
	}
}
