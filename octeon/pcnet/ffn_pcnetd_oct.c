/*
 * ffn_pcnetd -- OCTEON end of the FFN PCIe virtual Ethernet.
 *
 * Bridges a TAP interface (ffnnet0) to the two rings in local DRAM at
 * FFN_PCNET_BASE. The host reaches the same DRAM across PCIe through the BAR1
 * index-1 window; here it is just local memory, so every access is a cheap
 * cached load/store with a `sync` at the ordering points.
 *
 * Ownership: the HOST resets the region and publishes the magic. This side waits
 * for the magic, sets oct_up, and then runs. It never writes the header except
 * its own up-flag, so a host restart that re-resets the region is handled by
 * simply seeing the magic again.
 *
 * Built static (see the Makefile) so it drops into the lean initramfs with no
 * libc dependency, exactly like ffn_cpdpd -- but this one uses libc for the TAP
 * ioctls, which raw syscalls would make needlessly painful.
 *
 * The protocol is the reference in tools/ffn_pcnet_ring.py, already validated
 * against this DRAM through cpdp. All multi-byte control fields are big-endian;
 * on this big-endian CPU that means native, so no swaps appear below.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <linux/if.h>
#include <linux/if_tun.h>

#include "ffn_pcnet.h"

typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;

#define barrier() __asm__ __volatile__("sync" ::: "memory")

/* reflected crc32, matches zlib.crc32 and the host side */
static u32 crc32(const u8 *p, u32 n)
{
	u32 c = 0xffffffffu;
	u32 i;
	int k;

	for (i = 0; i < n; i++) {
		c ^= p[i];
		for (k = 0; k < 8; k++)
			c = (c >> 1) ^ (0xEDB88320u & (u32)(-(int)(c & 1)));
	}
	return ~c;
}

/* big-endian accessors: native on this CPU, but explicit so intent is clear */
static u32 be32(u32 v) { return v; }             /* CPU is big-endian */

static volatile u8 *region;

static volatile struct ffn_pcnet_ring *ring(u32 off)
{
	return (volatile struct ffn_pcnet_ring *)(region + off);
}

static volatile struct ffn_pcnet_slot *slot(u32 ring_off, u32 i)
{
	return (volatile struct ffn_pcnet_slot *)
		(region + ring_off + ffn_pcnet_slot_off(i));
}

/*
 * Consume one frame from a ring into `buf`. Returns the length, 0 if empty.
 * This is the consumer: it reads head, and advances tail.
 */
static u32 ring_get(u32 ring_off, u8 *buf, u32 cap)
{
	volatile struct ffn_pcnet_ring *r = ring(ring_off);
	u32 head = be32(r->head);
	u32 tail = be32(r->tail);
	volatile struct ffn_pcnet_slot *s;
	u32 len, crc, i;

	if (head == tail)
		return 0;
	s = slot(ring_off, tail);
	len = be32(s->len);
	if (len == 0)
		return 0;                /* producer advanced head, len not yet visible */
	if (len > cap || len > FFN_PCNET_SLOT - 8)
		len = 0;                 /* corrupt; drop by clearing below */
	for (i = 0; i < len; i++)
		buf[i] = s->data[i];
	crc = be32(s->crc);
	s->len = 0;                      /* release the slot */
	barrier();
	r->tail = be32((tail + 1) % FFN_PCNET_NSLOTS);
	barrier();
	if (len && crc32(buf, len) != crc)
		return 0;                /* detected coherency slip; drop */
	return len;
}

/*
 * Produce one frame into a ring. Returns 0 on success, -1 if the ring is full.
 * Producer: writes payload+crc+len, THEN advances head, with a barrier between
 * so the host never sees a head advance ahead of the payload.
 */
static int ring_put(u32 ring_off, const u8 *buf, u32 len)
{
	volatile struct ffn_pcnet_ring *r = ring(ring_off);
	u32 head = be32(r->head);
	u32 tail = be32(r->tail);
	volatile struct ffn_pcnet_slot *s;
	u32 i;

	if (len > FFN_PCNET_SLOT - 8)
		return -1;
	if ((head + 1) % FFN_PCNET_NSLOTS == tail) {
		r->producer_drops = be32(be32(r->producer_drops) + 1);
		return -1;
	}
	s = slot(ring_off, head);
	for (i = 0; i < len; i++)
		s->data[i] = buf[i];
	s->crc = be32(crc32(buf, len));
	barrier();
	s->len = be32(len);              /* ready flag, after the payload */
	barrier();
	r->head = be32((head + 1) % FFN_PCNET_NSLOTS);
	barrier();
	return 0;
}

static int tap_open(const char *name)
{
	struct ifreq ifr;
	int fd;

	/*
	 * The lean initramfs has no udev, so /dev/net/tun may not exist. Create
	 * it (misc major 10, tun minor 200) before opening. mkdir/mknod are
	 * idempotent enough here -- EEXIST is fine.
	 */
	mkdir("/dev/net", 0755);
	if (mknod("/dev/net/tun", S_IFCHR | 0600, makedev(10, 200)) < 0 &&
	    errno != EEXIST)
		fprintf(stderr, "ffn_pcnetd: mknod /dev/net/tun: %s\n",
			strerror(errno));

	fd = open("/dev/net/tun", O_RDWR);
	if (fd < 0)
		return -1;
	memset(&ifr, 0, sizeof(ifr));
	ifr.ifr_flags = IFF_TAP | IFF_NO_PI;
	strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
	if (ioctl(fd, TUNSETIFF, &ifr) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

/*
 * Configure the interface's IPv4 address, mask, MTU and bring it up, all via
 * ioctls -- the lean initramfs has no `ip` tool, and doing it here means init
 * only has to spawn this daemon. addr and mask are dotted-quad strings.
 */
static int inet_aton4(const char *s, uint32_t *out)
{
	uint32_t b[4] = {0, 0, 0, 0};
	int i = 0;

	for (; *s; s++) {
		if (*s >= '0' && *s <= '9')
			b[i] = b[i] * 10 + (uint32_t)(*s - '0');
		else if (*s == '.' && i < 3)
			i++;
		else
			return -1;
	}
	if (i != 3)
		return -1;
	*out = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3];
	return 0;
}

static void set_sockaddr(struct sockaddr *sa, uint32_t hostaddr)
{
	struct sockaddr_in *in = (struct sockaddr_in *)sa;

	memset(in, 0, sizeof(*in));
	in->sin_family = AF_INET;
	in->sin_addr.s_addr = htonl(hostaddr);
}

static int cfg_ip(const char *name, const char *addr, const char *mask, int mtu)
{
	struct ifreq ifr;
	uint32_t a, m;
	int s = socket(AF_INET, SOCK_DGRAM, 0);

	if (s < 0)
		return -1;
	if (inet_aton4(addr, &a) || inet_aton4(mask, &m)) {
		close(s);
		return -1;
	}
	memset(&ifr, 0, sizeof(ifr));
	strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);

	set_sockaddr(&ifr.ifr_addr, a);
	if (ioctl(s, SIOCSIFADDR, &ifr) < 0) { close(s); return -1; }
	set_sockaddr(&ifr.ifr_netmask, m);
	if (ioctl(s, SIOCSIFNETMASK, &ifr) < 0) { close(s); return -1; }

	ifr.ifr_mtu = mtu;
	ioctl(s, SIOCSIFMTU, &ifr);           /* non-fatal */

	if (ioctl(s, SIOCGIFFLAGS, &ifr) < 0) { close(s); return -1; }
	ifr.ifr_flags |= IFF_UP | IFF_RUNNING;
	if (ioctl(s, SIOCSIFFLAGS, &ifr) < 0) { close(s); return -1; }

	close(s);
	return 0;
}

int main(void)
{
	int memfd, tapfd;
	volatile struct ffn_pcnet_hdr *h;
	u8 frame[FFN_PCNET_SLOT];
	int spins = 0;

	memfd = open("/dev/mem", O_RDWR | O_SYNC);
	if (memfd < 0) {
		perror("open /dev/mem");
		return 1;
	}
	region = mmap(NULL, FFN_PCNET_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED,
		      memfd, FFN_PCNET_BASE);
	if (region == MAP_FAILED) {
		perror("mmap region");
		return 1;
	}
	h = (volatile struct ffn_pcnet_hdr *)region;

	/* Wait for the host to publish the magic (host owns region init). */
	fprintf(stderr, "ffn_pcnetd: waiting for host magic at 0x%llx...\n",
		(unsigned long long)FFN_PCNET_BASE);
	while (h->magic != FFN_PCNET_MAGIC) {
		usleep(100000);
		if (++spins > 600) {
			fprintf(stderr, "ffn_pcnetd: no host magic after 60s\n");
			return 1;
		}
	}
	h->oct_up = be32(1);
	barrier();

	tapfd = tap_open("ffnnet0");
	if (tapfd < 0) {
		perror("tap_open ffnnet0");
		return 1;
	}
	if (cfg_ip("ffnnet0", "127.1.1.2", "255.255.255.0", FFN_PCNET_MTU) < 0)
		fprintf(stderr, "ffn_pcnetd: WARNING ffnnet0 IP config failed (%s)\n",
			strerror(errno));
	else
		fprintf(stderr, "ffn_pcnetd: ffnnet0 = 127.1.1.2/24 up\n");

	/*
	 * 127/8 is non-routable by default -- which is exactly why the vendor uses
	 * 127.1.x for this link (it cannot be reached from any physical topology),
	 * but it means the kernel will not route to 127.1.1.1 without being told
	 * to. route_localnet lifts that just for this interface.
	 */
	{
		int pf = open("/proc/sys/net/ipv4/conf/ffnnet0/route_localnet",
			      O_WRONLY);
		if (pf >= 0) {
			if (write(pf, "1\n", 2) != 2)
				fprintf(stderr, "ffn_pcnetd: route_localnet write short\n");
			close(pf);
		}
	}
	fprintf(stderr, "ffn_pcnetd: up. magic ok, ffnnet0 configured. bridging.\n");

	/*
	 * Bridge loop. TAP is edge-driven (poll), the O2H ring is level (poll
	 * with a short timeout) because the host does not signal us. A doorbell
	 * could replace the timeout later; polling local DRAM is cheap.
	 */
	for (;;) {
		struct pollfd pfd;
		u32 len;
		ssize_t n;

		/* drain host -> OCTEON into the TAP */
		while ((len = ring_get(FFN_PCNET_H2O_OFF, frame, sizeof(frame)))) {
			ssize_t w = write(tapfd, frame, len);
			(void)w;
		}

		/* move any TAP frames out to the OCTEON -> host ring */
		pfd.fd = tapfd;
		pfd.events = POLLIN;
		if (poll(&pfd, 1, 1) > 0 && (pfd.revents & POLLIN)) {
			n = read(tapfd, frame, sizeof(frame));
			if (n > 0)
				ring_put(FFN_PCNET_O2H_OFF, frame, (u32)n);
		}
	}
	return 0;
}
