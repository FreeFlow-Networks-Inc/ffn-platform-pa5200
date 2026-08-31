/*
 * ffn_dpagent2 -- persistent shell session for the DP Octeon, over PCIe.
 *
 * v1 (ffn_dpagent.c) was request/response: every command was a fresh "sh -c", so
 * nothing persisted -- no cwd, no shell variables, no interactive programs. This
 * version runs ONE long-lived shell on a pty and carries the raw byte stream both
 * ways through two rings in DP DRAM, so the session behaves like a terminal: cd
 * sticks, vi and top work, ^C and job control work.
 *
 * The DP's console is its own ttyS0, which nothing on the CP can read, so these
 * rings are the only way in. The CP drives them with ffn_dpsh.py.
 *
 * LAYOUT (DP phys 0x00400000, 64 KB; fields big-endian, native to this side).
 * Each 8-byte group is written by exactly ONE side. That is not cosmetic: the CP
 * reaches this memory through a window that byte-reverses each aligned 64-bit word,
 * so its writes are read-modify-write over a whole group. A CP-written field
 * sharing a group with a DP-written field would clobber concurrent DP updates.
 * Hence the padding.
 *
 *   0x0000  magic "FFNDPSH2"              (DP)
 *   0x0008  u32 version=2, u32 gen        (DP)  gen++ each agent start
 *   0x0010  u32 in_head,  pad             (CP)  bytes CP -> DP
 *   0x0018  u32 in_tail,  pad             (DP)
 *   0x0020  u32 out_head, pad             (DP)  bytes DP -> CP
 *   0x0028  u32 out_tail, pad             (CP)
 *   0x0030  u32 agent_up, u32 shell_alive (DP)
 *   0x0038  u32 in_size,  u32 out_size    (DP)
 *   0x1000  in  ring, 0x2000 bytes
 *   0x4000  out ring, 0xC000 bytes
 *
 * Head/tail are MONOTONIC 32-bit counters, not offsets: index = counter % size.
 * That removes the full-vs-empty ambiguity, and unsigned arithmetic keeps working
 * across the 2^32 wrap.
 *
 * This is the DP's only userspace process and its only control path, so it must
 * never exit: the shell is respawned if it dies and the poll loop has no exit path.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <termios.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/sysmacros.h>
#include <stdint.h>

#define RING_BASE   0x00400000UL
#define RING_SIZE   0x10000UL

#define OFF_MAGIC     0x0000
#define OFF_VERSION   0x0008
#define OFF_GEN       0x000c
#define OFF_IN_HEAD   0x0010
#define OFF_IN_TAIL   0x0018
#define OFF_OUT_HEAD  0x0020
#define OFF_OUT_TAIL  0x0028
#define OFF_AGENT_UP  0x0030
#define OFF_SH_ALIVE  0x0034
#define OFF_IN_SIZE   0x0038
#define OFF_OUT_SIZE  0x003c

#define IN_OFF   0x1000
#define IN_SIZE  0x2000
#define OUT_OFF  0x4000
#define OUT_SIZE 0x8000	/* MUST be a power of two: the index is
				 * counter % OUT_SIZE and the counter wraps at
				 * 2^32. 0xC000 made that map discontinuous at
				 * the wrap (2^32 %% 0xC000 = 0x4000). */

static volatile unsigned char *ring;

/*
 * Counter access is 64-bit-atomic on purpose.
 *
 * The CP reads these fields through a PCIe window that byte-reverses each aligned
 * 64-bit word, so it necessarily loads a WHOLE 8-byte group at a time. The previous
 * wr32() published a counter as four separate byte stores, which the CP could catch
 * half-updated: taking out_head from 0x0000FFFF to 0x00010000 stores MSB-first, so a
 * read landing after two bytes yields 0x0001FFFF -- forward of the truth. The tear
 * itself is real and worth removing. (An earlier comment here went further and
 * claimed it caused an unrecoverable tail-past-head overshoot; adversarial review
 * showed the call-site geometry prevents that particular consequence, so the tear is
 * a latent hazard rather than a demonstrated failure path.)
 *
 * Fix: one 64-bit load and one 64-bit store, so the CP can never observe a partial
 * value. The read-modify-write is safe because every group these functions touch is
 * written ONLY by this side (see the layout comment) -- the CP owns in_head and
 * out_tail and we never write those. Reading as one 64-bit load also closes the
 * mirror case, where a byte-at-a-time read here could tear a CP-written counter.
 *
 * Big-endian: the u32 at a group's base occupies the HIGH half of the u64; the one
 * at +4 occupies the low half.
 */
static uint32_t rd32(unsigned off)
{
	volatile uint64_t *g = (volatile uint64_t *)(ring + (off & ~7u));
	uint64_t cur = *g;

	return (off & 4u) ? (uint32_t)cur : (uint32_t)(cur >> 32);
}

static void wr32(unsigned off, uint32_t v)
{
	volatile uint64_t *g = (volatile uint64_t *)(ring + (off & ~7u));
	uint64_t cur = *g;

	if (off & 4u)
		cur = (cur & 0xffffffff00000000ull) | (uint64_t)v;
	else
		cur = (cur & 0x00000000ffffffffull) | ((uint64_t)v << 32);
	*g = cur;
}

/*
 * Store barrier before publishing a head/tail.
 *
 * NOTE: this is belt-and-braces, not a visibility fix. /dev/mem is opened with
 * O_SYNC, which Linux maps UNCACHED (uncached_access -> pgprot_noncached), so these
 * stores already reach memory in program order; cnMIPS L1 is write-through and L2C
 * is the coherence point for inbound PCIe reads. An earlier comment here claimed the
 * barrier was required for the CP to see the payload -- that was wrong. It is kept
 * because it costs nothing on this path and documents the ordering the protocol
 * depends on.
 */
static inline void dp_sync(void)
{
	__asm__ __volatile__("sync" ::: "memory");
}

/* Pull up to max bytes out of the CP->DP ring. */
static size_t in_take(unsigned char *dst, size_t max)
{
	uint32_t head = rd32(OFF_IN_HEAD);
	uint32_t tail = rd32(OFF_IN_TAIL);
	uint32_t avail = head - tail;              /* unsigned: wraps correctly */
	size_t n, i;

	if (!avail)
		return 0;
	if (avail > IN_SIZE)                       /* CP overran us; resync */
		avail = IN_SIZE;
	n = avail < max ? avail : max;
	for (i = 0; i < n; i++)
		dst[i] = ring[IN_OFF + ((uint32_t)(tail + (uint32_t)i) % IN_SIZE)];
	dp_sync();		/* our reads of the payload complete first */
	wr32(OFF_IN_TAIL, tail + (uint32_t)n);
	return n;
}

/* Push bytes into the DP->CP ring. If the CP is not draining, head runs ahead and
 * the CP resyncs on seeing head - tail > size; bounded either way. */
static size_t out_space(void)
{
	uint32_t head = rd32(OFF_OUT_HEAD);
	uint32_t tail = rd32(OFF_OUT_TAIL);
	uint32_t used;

	/*
	 * If the CP's tail has run AHEAD of our head the CP has mis-tracked. Reading
	 * that as a full ring would WEDGE us permanently: out_space() would return 0,
	 * the poll loop would stop draining the pty, and head could never advance to
	 * catch up. Re-sync to the CP instead -- losing the few bytes in flight is
	 * strictly better than losing the session, which is the only way in.
	 */
	if ((int32_t)(head - tail) < 0) {
		wr32(OFF_OUT_HEAD, tail);
		return OUT_SIZE;
	}

	used = head - tail;
	if (used >= OUT_SIZE)
		return 0;
	return OUT_SIZE - used;
}

/* Write only what fits and return how much was written. The caller must not read
 * more from the pty than out_space() allows, so nothing is ever silently dropped --
 * the bytes wait in the pty buffer instead. */
static size_t out_put(const unsigned char *src, size_t n)
{
	uint32_t head = rd32(OFF_OUT_HEAD);
	size_t space = out_space();
	size_t i;

	if (n > space)
		n = space;
	for (i = 0; i < n; i++)
		ring[OUT_OFF + ((uint32_t)(head + (uint32_t)i) % OUT_SIZE)] = src[i];
	if (n)
	dp_sync();		/* payload visible before we advertise it */
		wr32(OFF_OUT_HEAD, head + (uint32_t)n);
	return n;
}

/* Allocate a pty and start a shell on it. Returns the master fd, -1 on failure. */
static int start_shell(pid_t *out_pid)
{
	int master, slave;
	char *sname;
	pid_t pid;

	master = open("/dev/ptmx", O_RDWR | O_NOCTTY);
	if (master < 0) {
		fprintf(stderr, "ffn_dpagent2: open /dev/ptmx: %s\n",
			strerror(errno));
		return -1;
	}
	if (grantpt(master) != 0 || unlockpt(master) != 0) {
		fprintf(stderr, "ffn_dpagent2: grantpt/unlockpt: %s\n",
			strerror(errno));
		close(master);
		return -1;
	}
	sname = ptsname(master);
	if (!sname) {
		fprintf(stderr, "ffn_dpagent2: ptsname failed\n");
		close(master);
		return -1;
	}

	pid = fork();
	if (pid < 0) {
		fprintf(stderr, "ffn_dpagent2: fork: %s\n", strerror(errno));
		close(master);
		return -1;
	}
	if (pid == 0) {
		setsid();
		slave = open(sname, O_RDWR);
		if (slave < 0)
			_exit(127);
		ioctl(slave, TIOCSCTTY, 0);
		dup2(slave, 0);
		dup2(slave, 1);
		dup2(slave, 2);
		if (slave > 2)
			close(slave);
		close(master);
		setenv("TERM", "vt100", 1);
		setenv("PS1", "dp# ", 1);
		setenv("HOME", "/", 1);
		setenv("PATH", "/bin:/sbin:/usr/bin:/usr/sbin", 1);
		execl("/bin/sh", "sh", "-i", (char *)NULL);
		_exit(127);
	}

	fcntl(master, F_SETFL, O_NONBLOCK);
	*out_pid = pid;
	return master;
}

int main(void)
{
	int fd, master;
	pid_t shpid = -1;
	unsigned char buf[4096];

	if (mknod("/dev/mem", S_IFCHR | 0600, makedev(1, 1)) != 0 &&
	    errno != EEXIST)
		fprintf(stderr, "ffn_dpagent2: mknod /dev/mem: %s\n",
			strerror(errno));
	/* Unix98 ptys need /dev/ptmx and devpts mounted; the lean initramfs has
	 * neither. Harmless if already present. */
	if (mknod("/dev/ptmx", S_IFCHR | 0666, makedev(5, 2)) != 0 &&
	    errno != EEXIST)
		fprintf(stderr, "ffn_dpagent2: mknod /dev/ptmx: %s\n",
			strerror(errno));
	mkdir("/dev/pts", 0755);
	if (mount("devpts", "/dev/pts", "devpts", 0, "mode=0620") != 0 &&
	    errno != EBUSY)
		fprintf(stderr, "ffn_dpagent2: mount devpts: %s\n",
			strerror(errno));

	signal(SIGPIPE, SIG_IGN);

	fd = open("/dev/mem", O_RDWR | O_SYNC);
	if (fd < 0) {
		fprintf(stderr, "ffn_dpagent2: open /dev/mem: %s\n",
			strerror(errno));
		return 1;
	}
	ring = mmap(NULL, RING_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
		    fd, (off_t)RING_BASE);
	if (ring == MAP_FAILED) {
		fprintf(stderr, "ffn_dpagent2: mmap 0x%lx: %s\n",
			RING_BASE, strerror(errno));
		return 1;
	}

	/* Publish the header, magic LAST so the CP never sees it half-built. */
	{
		uint32_t gen = rd32(OFF_GEN);

		wr32(OFF_IN_TAIL, rd32(OFF_IN_HEAD));      /* start empty */
		wr32(OFF_OUT_HEAD, rd32(OFF_OUT_TAIL));
		wr32(OFF_IN_SIZE, IN_SIZE);
		wr32(OFF_OUT_SIZE, OUT_SIZE);
		wr32(OFF_SH_ALIVE, 0);
		wr32(OFF_AGENT_UP, 1);
		wr32(OFF_VERSION, 2);
		wr32(OFF_GEN, gen + 1);
		memcpy((void *)(ring + OFF_MAGIC), "FFNDPSH2", 8);
	}

	master = start_shell(&shpid);
	wr32(OFF_SH_ALIVE, master >= 0 ? 1 : 0);

	for (;;) {
		int busy = 0;
		ssize_t n;
		size_t got;

		/* CP keystrokes -> the shell */
		got = in_take(buf, sizeof(buf));
		if (got && master >= 0) {
			size_t done = 0;

			while (done < got) {
				n = write(master, buf + done, got - done);
				if (n > 0) {
					done += (size_t)n;
				} else if (n < 0 && (errno == EAGAIN ||
						     errno == EINTR)) {
					usleep(1000);
				} else {
					break;
				}
			}
			busy = 1;
		}

		/* shell output -> the CP, but only as much as the ring can hold:
		 * reading more would force us to drop it. */
		if (master >= 0 && out_space() >= 512) {
			size_t room = out_space();

			if (room > sizeof(buf))
				room = sizeof(buf);
			n = read(master, buf, room);
			if (n > 0) {
				out_put(buf, (size_t)n);
				busy = 1;
			} else if (n == 0) {
				close(master);
				master = -1;
			} else if (errno != EAGAIN && errno != EINTR) {
				close(master);
				master = -1;
			}
		}

		/* reap, and respawn so the session survives 'exit' */
		if (shpid > 0) {
			int st;

			if (waitpid(shpid, &st, WNOHANG) == shpid) {
				shpid = -1;
				if (master >= 0) {
					close(master);
					master = -1;
				}
			}
		}
		if (master < 0) {
			static const char msg[] =
				"\r\n[ffn_dpagent2: shell exited, restarting]\r\n";

			(void)out_put((const unsigned char *)msg, sizeof(msg) - 1);
			wr32(OFF_SH_ALIVE, 0);
			sleep(1);
			master = start_shell(&shpid);
			wr32(OFF_SH_ALIVE, master >= 0 ? 1 : 0);
			busy = 1;
		}

		if (!busy)
			usleep(2000);      /* 2 ms idle poll */
	}
	return 0;
}
