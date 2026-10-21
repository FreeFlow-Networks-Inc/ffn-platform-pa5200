/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _GNU_SOURCE
#include "ffn_packet.h"
#include "ffn_fe100_stats.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/if_packet.h>
#include <linux/if_tun.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <poll.h>
#include <pthread.h>
#include <stdatomic.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/eventfd.h>
#include <time.h>
#include <unistd.h>

#define MAX_FRAME 1518
#define BURST 64
enum { RX, TX, ENVELOPE_DROP, LENGTH_DROP, PRESSURE_DROP, ADMIN_DROP,
       LOCAL_INPUT, INSPECTION_DROP, BYPASSED, UNSUPPORTED, MALFORMED,
       NO_MATCH, ALERT, BLOCK, OUTGOING, BAD_VERDICT };

/* A lane is one receive socket, one TAP queue and one transmit socket with a
 * receive worker and a transmit worker of their own. Lane 0 is the owner as it
 * always was; more lanes share the trunk's fanout group, which hands every
 * frame to the lane named by the receive queue the trunk driver recorded (the
 * SSO group its flow hashed to), so a flow stays in order on one lane while
 * flows spread. Each lane writes its own TAP queue, so no lock serializes the
 * lanes in the kernel either. */
struct ffn_lane {
    int rx, tx, tap, wake[2];
    /* Stable heap-backed buffers: no per-packet allocation or large worker stack. */
    struct mmsghdr messages[BURST];
    struct iovec vectors[BURST];
    struct sockaddr_ll addresses[BURST];
    uint8_t frames[BURST][MAX_FRAME + 81];
    struct mmsghdr tx_messages[BURST];
    struct iovec tx_vectors[BURST];
    uint8_t tx_frames[BURST][MAX_FRAME + 12];
    pthread_t threads[2];
    struct { struct ffn_packet *owner; unsigned lane, direction; } arguments[2];
    unsigned started;
    int paused[2], cpus[2], tids[2];
};
struct ffn_packet {
    struct ffn_lane *lanes[FFN_PACKET_LANES_MAX];
    unsigned lane_count;
    /* What more lanes need: the names ffn_packet_open was given, the receive
     * sources and filter mode of lane 0, and whether the TAP takes queues. */
    char wire[IFNAMSIZ], netns[IFNAMSIZ], tapname[IFNAMSIZ];
    uint32_t sources[16];
    unsigned source_count;
    int mode, multiqueue, opened;
    struct ffn_packet_fe100_binding fe100[8];
    unsigned fe100_count;
    uint64_t fe100_deadline;
    _Atomic uint64_t fe100_stats[3]; /* decoded, rejected, expired */
    _Atomic uint64_t receive_calls, receive_frames, receive_max_batch;
    _Atomic uint64_t transmit_calls, transmit_frames, transmit_max_batch;
    struct ffn_aggregate *aggregate;
    unsigned source, n4, n6;
    uint8_t local4[FFN_PACKET_LOCAL_MAX][4], local6[FFN_PACKET_LOCAL_MAX][16];
    void *inspection;
    ffn_scan_fn scan;
    _Atomic uint64_t counters[FFN_PACKET_COUNTERS];
    pthread_mutex_t control;
    pthread_cond_t changed;
    unsigned started;
    int pause_requested, stopped, fault;
    _Atomic int carrier;             /* physical port: deliver received frames (ffn_packet_carrier) */
    _Atomic uint64_t carrier_drops;
    int64_t lease_end;
};

/* How long the workers run without the owner resuming or renewing them. */
#define LEASE_MS 3000

unsigned ffn_packet_abi(void) { return FFN_PACKET_ABI; }
static uint16_t be16(const uint8_t *p) { return (uint16_t)p[0] << 8 | p[1]; }
static int64_t milliseconds(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static struct ffn_lane *lane_new(void)
{
    struct ffn_lane *l = calloc(1, sizeof(*l));
    if (!l) return NULL;
    l->rx = l->tx = l->tap = l->wake[0] = l->wake[1] = -1;
    for (unsigned i = 0; i < BURST; i++) {
        l->vectors[i] = (struct iovec){l->frames[i], sizeof(l->frames[i])};
        l->messages[i].msg_hdr.msg_iov = &l->vectors[i];
        l->messages[i].msg_hdr.msg_iovlen = 1;
        l->messages[i].msg_hdr.msg_name = &l->addresses[i];
        l->tx_vectors[i].iov_base = l->tx_frames[i];
        l->tx_messages[i].msg_hdr.msg_iov = &l->tx_vectors[i];
        l->tx_messages[i].msg_hdr.msg_iovlen = 1;
    }
    return l;
}
static void lane_free(struct ffn_lane *l)
{
    if (!l) return;
    if (l->rx >= 0) close(l->rx);
    if (l->tx >= 0) close(l->tx);
    if (l->tap >= 0) close(l->tap);
    for (unsigned i = 0; i < 2; i++) if (l->wake[i] >= 0) close(l->wake[i]);
    free(l);
}
/* Every started worker is parked at the barrier (or none was started). */
static int all_paused(const struct ffn_packet *p)
{
    for (unsigned i = 0; i < p->lane_count; i++)
        for (unsigned d = 0; d < p->lanes[i]->started; d++)
            if (!p->lanes[i]->paused[d]) return 0;
    return 1;
}
void ffn_packet_close(struct ffn_packet *p)
{
    if (!p) return;
    pthread_mutex_lock(&p->control);
    p->stopped = 1; pthread_cond_broadcast(&p->changed);
    pthread_mutex_unlock(&p->control);
    for (unsigned i = 0; i < p->lane_count; i++)
        for (unsigned d = 0; d < p->lanes[i]->started; d++) pthread_join(p->lanes[i]->threads[d], NULL);
    for (unsigned i = 0; i < p->lane_count; i++) lane_free(p->lanes[i]);
    free(p->aggregate);
    pthread_cond_destroy(&p->changed); pthread_mutex_destroy(&p->control); free(p);
}
static struct ffn_packet *allocate(unsigned source)
{
    struct ffn_packet *p;
    if (source > 65535) { errno = EINVAL; return NULL; }
    p = calloc(1, sizeof(*p));
    if (p) {
        int rc;
        p->source = source;
        atomic_store_explicit(&p->carrier, 1, memory_order_relaxed); /* delivering until an owner says otherwise */
        p->lanes[0] = lane_new();
        if (!p->lanes[0]) { free(p); return NULL; }
        p->lane_count = 1;
        rc = pthread_mutex_init(&p->control, NULL);
        if (rc) { lane_free(p->lanes[0]); free(p); errno = rc; return NULL; }
        rc = pthread_cond_init(&p->changed, NULL);
        if (rc) { pthread_mutex_destroy(&p->control); lane_free(p->lanes[0]); free(p); errno = rc; return NULL; }
    }
    return p;
}
static int nonblock(int fd)
{
    int flags = fcntl(fd, F_GETFL);
    return flags < 0 ? -1 : fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}
static int lane_adopt(struct ffn_lane *l, int rx, int tx, int tap)
{
    l->rx = fcntl(rx, F_DUPFD_CLOEXEC, 3);
    l->tx = fcntl(tx, F_DUPFD_CLOEXEC, 3);
    l->tap = fcntl(tap, F_DUPFD_CLOEXEC, 3);
    return l->rx < 0 || l->tx < 0 || l->tap < 0 || nonblock(l->rx) || nonblock(l->tx) || nonblock(l->tap) ? -1 : 0;
}
struct ffn_packet *ffn_packet_adopt(int rx, int tx, int tap, unsigned source)
{
    struct ffn_packet *p = allocate(source);
    if (!p) return NULL;
    if (lane_adopt(p->lanes[0], rx, tx, tap)) {
        int error = errno; ffn_packet_close(p); errno = error; return NULL;
    }
    return p;
}
/* One more lane from caller-owned descriptors, before the workers start:
 * isolated transport validation of the lane machinery itself. */
int ffn_packet_adopt_lane(struct ffn_packet *p, int rx, int tx, int tap)
{
    struct ffn_lane *l;
    if (!p || p->started || p->lane_count >= FFN_PACKET_LANES_MAX) { errno = EINVAL; return -1; }
    l = lane_new();
    if (!l) return -1;
    if (lane_adopt(l, rx, tx, tap)) { int error = errno; lane_free(l); errno = error; return -1; }
    p->lanes[p->lane_count++] = l;
    return 0;
}
static int valid_name(const char *s)
{
    size_t n;
    if (!s || !(n = strlen(s)) || n >= IFNAMSIZ) return 0;
    return strspn(s, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-") == n
           && strcmp(s, ".") && strcmp(s, "..");
}
/* The owner's receive filter. Ordinary frames pass by their source system port.
 * The FE100's returns (port 20 copies on the trunk) are shared by every owner on
 * the trunk, so they pass by the front port their packet info names: after the
 * IPv4 (0x40, 24-byte) or IPv6 (0x80, 48-byte) key of a FLOWUNKNOWN message, or
 * directly for exception and control messages. One message then reaches one
 * owner, and a rejection counted by the decoder is a malformed message for a
 * front the owner holds, never another owner's traffic. */
static int packet_filter(int fd, const uint32_t *sources, unsigned count, int mode,
                         const uint32_t *returns, unsigned return_count,
                         const uint32_t *fronts, unsigned front_count)
{
    struct sock_filter instructions[96];
    struct sock_fprog program = { .filter = instructions };
    unsigned used = 0, drop;
    if (count > 16 || !sources || !count || return_count > 8 || (return_count && !returns) ||
        front_count > 8 || (return_count && (!front_count || !fronts)) ||
        (!return_count && front_count)) { errno = EINVAL; return -1; }
#define INS(code, jt, jf, k) instructions[used++] = (struct sock_filter){code, jt, jf, k}
#define JUMP(from, to) ((uint8_t)((to) - (from) - 1))
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 0);
    INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 24);
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 2);
    for (unsigned i = 0; i < return_count; i++)
        INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, returns[i]);           /* jt: the return demux */
    for (unsigned i = 0; i < count; i++) {
        if (sources[i] > 65535) { errno = EINVAL; return -1; }
        INS(BPF_JMP | BPF_JEQ | BPF_K, count - i, 0, sources[i]);   /* past the drop */
    }
    drop = used; INS(BPF_RET | BPF_K, 0, 0, 0);
    instructions[1].jf = JUMP(1, drop);
    if (mode == 2) { /* Slow protocol control frames only. */
        INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 16);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 2, 0, 0x8809);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, 0x88cc);
        INS(BPF_RET | BPF_K, 0, 0, 0);
    } else if (mode == 3) { /* No LACP/LLDP copies in the data queue. */
        INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 16);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, 0x8809);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, 0x88cc);
        INS(BPF_RET | BPF_K, 0, 0, 0);
    }
    INS(BPF_RET | BPF_K, 0, 0, 65535);
    if (return_count) {
        unsigned demux, j_type, j_code, j_v4, j_v6, at_v4, ja_v4, at_v6, ja_v6, nokey, match, first, raccept;
        demux = used; INS(BPF_LD | BPF_B | BPF_ABS, 0, 0, 11);      /* message type */
        j_type = used; INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 8);
        INS(BPF_LD | BPF_B | BPF_ABS, 0, 0, 31);                    /* exception code */
        j_code = used; INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 0);
        INS(BPF_LD | BPF_B | BPF_ABS, 0, 0, 32);                    /* FLOWUNKNOWN key kind */
        j_v4 = used; INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 0x40);
        j_v6 = used; INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 0x80);
        at_v4 = used; INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 48);      /* packet info after the IPv4 key */
        ja_v4 = used; INS(BPF_JMP | BPF_JA, 0, 0, 0);
        at_v6 = used; INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 72);      /* after the IPv6 key */
        ja_v6 = used; INS(BPF_JMP | BPF_JA, 0, 0, 0);
        nokey = used; INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 32);      /* exceptions and control: no key */
        match = used; INS(BPF_ALU | BPF_AND | BPF_K, 0, 0, 0xffc0); /* reserved bits clear, port bits 6..11 */
        first = used;
        for (unsigned i = 0; i < front_count; i++) {
            if (!fronts[i] || fronts[i] > 63) { errno = EINVAL; return -1; }
            INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, fronts[i] << 6);   /* jt: this owner's return */
        }
        INS(BPF_RET | BPF_K, 0, 0, 0);
        raccept = used;
        if (mode == 2) {
            /* Qualified NOTFLOW/ACL control envelopes carry Ethernet at +40.
             * Do not copy high-volume FE100 data misses into the LACP queue. */
            INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 52);
            INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, 0x8809);
            INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, 0x88cc);
            INS(BPF_RET | BPF_K, 0, 0, 65535);
            INS(BPF_RET | BPF_K, 0, 0, 0);
        } else INS(BPF_RET | BPF_K, 0, 0, 65535);
        instructions[j_type].jf = JUMP(j_type, nokey);
        instructions[j_code].jf = JUMP(j_code, nokey);
        instructions[j_v4].jt = JUMP(j_v4, at_v4);
        instructions[j_v6].jt = JUMP(j_v6, at_v6); instructions[j_v6].jf = JUMP(j_v6, nokey);
        instructions[ja_v4].k = JUMP(ja_v4, match); instructions[ja_v6].k = JUMP(ja_v6, match);
        for (unsigned i = 0; i < front_count; i++) instructions[first + i].jt = JUMP(first + i, raccept);
        for (unsigned i = 0; i < return_count; i++) instructions[3 + i].jt = JUMP(3 + i, demux);
    }
    program.len = (unsigned short)used;
#undef JUMP
#undef INS
    return setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &program, sizeof(program));
}
static int packet_socket_many(const char *name, const uint32_t *sources, unsigned count, int mode)
{
    unsigned index = if_nametoindex(name);
    struct sockaddr_ll addr = { .sll_family = AF_PACKET, .sll_ifindex = (int)index };
    int fd, yes = 1, buffer = 4 * 1024 * 1024, error;
    if (!index) { errno = ENODEV; return -1; }
    fd = socket(AF_PACKET, SOCK_RAW | SOCK_CLOEXEC | SOCK_NONBLOCK, mode ? htons(3) : 0);
    if (fd < 0) return -1;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr))) goto fail;
    if (mode && (packet_filter(fd, sources, count, mode, NULL, 0, NULL, 0) ||
        setsockopt(fd, SOL_PACKET, PACKET_IGNORE_OUTGOING, &yes, sizeof(yes)) ||
        setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &buffer, sizeof(buffer)))) goto fail;
    return fd;
fail:
    error = errno; close(fd); errno = error; return -1;
}
static int packet_socket(const char *name, unsigned source, int receive)
{
    uint32_t sources[] = {source};
    return packet_socket_many(name, sources, 1, receive);
}
/* The kernel drops a frame it cannot queue on the TAP (tx_dropped) when the
 * backlog toward this reader reaches the device's transmit queue length. The
 * default of 1000 lost 630..960 frames per 15 s under a 700 Mbit/s download on
 * the PA-5220 (0.1 %, enough to hold parallel TCP streams back) although the
 * reader idled on average: it is microbursts, not throughput. 10000 held zero.
 * Set it where the reader attaches, so every owner gets it whoever created the
 * device; the TAP is already in the target namespace at that point. */
#define FFN_PACKET_TAP_QUEUE 10000
static int tap_queue_depth(const char *tap, unsigned depth)
{
    struct ifreq request; int sock, result, error;
    sock = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (sock < 0) return -1;
    memset(&request, 0, sizeof(request));
    memcpy(request.ifr_name, tap, strlen(tap) + 1);
    request.ifr_qlen = (int)depth;
    result = ioctl(sock, SIOCSIFTXQLEN, &request); error = errno;
    close(sock); errno = error;
    return result;
}
/* One queue of the TAP, opened inside the forwarding namespace. A device made
 * with multi_queue takes a queue per attach and refuses a plain attach; a plain
 * device refuses IFF_MULTI_QUEUE. The first attach learns which it is. */
static int tap_queue_open(struct ffn_packet *p)
{
    struct ifreq request = {0};
    char path[128];
    int original = -1, target = -1, switched = 0, fd = -1, error;
    original = open("/proc/self/ns/net", O_RDONLY | O_CLOEXEC);
    snprintf(path, sizeof(path), "/run/netns/%s", p->netns);
    target = open(path, O_RDONLY | O_CLOEXEC);
    if (original < 0 || target < 0 || setns(target, CLONE_NEWNET)) goto fail;
    switched = 1;
    if (!if_nametoindex(p->tapname)) { errno = ENODEV; goto fail; }
    fd = open("/dev/net/tun", O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) goto fail;
    memcpy(request.ifr_name, p->tapname, strlen(p->tapname) + 1);
    request.ifr_flags = IFF_TAP | IFF_NO_PI | IFF_MULTI_QUEUE;
    if (ioctl(fd, TUNSETIFF, &request)) {
        if (p->opened || errno != EINVAL) goto fail;
        request.ifr_flags = IFF_TAP | IFF_NO_PI;
        if (ioctl(fd, TUNSETIFF, &request)) goto fail;
        p->multiqueue = 0;
    } else if (!p->opened) p->multiqueue = 1;
    if (!p->opened && tap_queue_depth(p->tapname, FFN_PACKET_TAP_QUEUE)) goto fail;
    if (setns(original, CLONE_NEWNET)) goto fail;
    close(original); close(target);
    return fd;
fail:
    error = errno;
    if (switched && setns(original, CLONE_NEWNET)) error = errno;
    if (original >= 0) close(original);
    if (target >= 0) close(target);
    if (fd >= 0) close(fd);
    errno = error; return -1;
}
static int lane_open(struct ffn_packet *p, struct ffn_lane *l)
{
    l->rx = packet_socket_many(p->wire, p->sources, p->source_count, p->mode);
    if (l->rx < 0) return -1;
    l->tx = packet_socket(p->wire, p->source, 0);
    if (l->tx < 0) return -1;
    l->tap = tap_queue_open(p);
    return l->tap < 0 ? -1 : 0;
}
struct ffn_packet *ffn_packet_open(const char *wire, const char *namespace,
                                    const char *tap, unsigned source)
{
    struct ffn_packet *p;
    if (!valid_name(wire) || !valid_name(namespace) || !valid_name(tap)) {
        errno = EINVAL; return NULL;
    }
    p = allocate(source); if (!p) return NULL;
    memcpy(p->wire, wire, strlen(wire) + 1);
    memcpy(p->netns, namespace, strlen(namespace) + 1);
    memcpy(p->tapname, tap, strlen(tap) + 1);
    p->sources[0] = source; p->source_count = 1; p->mode = 1;
    if (lane_open(p, p->lanes[0])) {
        int error = errno; ffn_packet_close(p); errno = error; return NULL;
    }
    p->opened = 1;
    return p;
}
/* Grow the owner to `count` lanes before its workers start. Returns the lane
 * count reached: 1 when the TAP was not made with multi_queue (the owner then
 * runs exactly as before), else `count`. Every lane's receive socket joins one
 * fanout group of the kernel's choosing, demultiplexed by the receive queue
 * the trunk driver records; an older driver records none, and every frame
 * then reaches lane 0 alone, which is merely the old behaviour. */
int ffn_packet_lanes(struct ffn_packet *p, unsigned count)
{
    unsigned id = 0;
    if (!p || !count || count > FFN_PACKET_LANES_MAX || p->started) { errno = EINVAL; return -1; }
    /* Adopted descriptors and a single-queue TAP both mean: the lanes there are. */
    if (count <= p->lane_count || !p->opened || !p->multiqueue) return (int)p->lane_count;
    while (p->lane_count < count) {
        struct ffn_lane *l = lane_new();
        if (!l) return -1;
        if (lane_open(p, l)) { int error = errno; lane_free(l); errno = error; return -1; }
        p->lanes[p->lane_count++] = l;
    }
    for (unsigned i = 0; i < p->lane_count; i++) {
        unsigned argument = i ? id | (PACKET_FANOUT_QM << 16)
                              : (PACKET_FANOUT_QM | PACKET_FANOUT_FLAG_UNIQUEID) << 16;
        socklen_t size = sizeof(argument);
        if (setsockopt(p->lanes[i]->rx, SOL_PACKET, PACKET_FANOUT, &argument, sizeof(argument))) return -1;
        if (!i) {
            if (getsockopt(p->lanes[i]->rx, SOL_PACKET, PACKET_FANOUT, &argument, &size)) return -1;
            id = argument & 0xffff;
        }
    }
    return (int)p->lane_count;
}
unsigned ffn_packet_lane_count(const struct ffn_packet *p) { return p ? p->lane_count : 0; }
int ffn_packet_configure(struct ffn_packet *p, const uint8_t *v4, unsigned n4,
                         const uint8_t *v6, unsigned n6, void *inspection, ffn_scan_fn scan)
{
    if (!p || n4 > FFN_PACKET_LOCAL_MAX || n6 > FFN_PACKET_LOCAL_MAX ||
        (n4 && !v4) || (n6 && !v6) || (!!inspection != !!scan)) {
        errno = EINVAL; return -1;
    }
    pthread_mutex_lock(&p->control);
    if (p->started && (!p->pause_requested || !all_paused(p))) {
        pthread_mutex_unlock(&p->control); errno = EBUSY; return -1;
    }
    if (n4) memcpy(p->local4, v4, n4 * 4);
    if (n6) memcpy(p->local6, v6, n6 * 16);
    p->n4 = n4; p->n6 = n6; p->inspection = inspection; p->scan = scan;
    pthread_mutex_unlock(&p->control); return 0;
}
static int allow(struct ffn_packet *p, const uint8_t *frame, size_t size)
{
    unsigned i; int verdict;
    if (size >= 34 && be16(frame + 12) == 0x0800 && frame[14] >> 4 == 4) {
        for (i = 0; i < p->n4; i++) if (!memcmp(frame + 30, p->local4[i], 4)) goto local;
    } else if (size >= 54 && be16(frame + 12) == 0x86dd && frame[14] >> 4 == 6) {
        for (i = 0; i < p->n6; i++) if (!memcmp(frame + 38, p->local6[i], 16)) goto local;
    }
    if (!p->scan) { atomic_fetch_add_explicit(&p->counters[BYPASSED], 1, memory_order_relaxed); return 1; }
    verdict = p->scan(p->inspection, (const char *)frame, (unsigned)size);
    switch (verdict) {
    case -2: atomic_fetch_add_explicit(&p->counters[UNSUPPORTED], 1, memory_order_relaxed); return 1;
    case -1: atomic_fetch_add_explicit(&p->counters[MALFORMED], 1, memory_order_relaxed); return 1;
    case 0: atomic_fetch_add_explicit(&p->counters[NO_MATCH], 1, memory_order_relaxed); return 1;
    case 1: atomic_fetch_add_explicit(&p->counters[ALERT], 1, memory_order_relaxed); return 1;
    case 2: atomic_fetch_add_explicit(&p->counters[BLOCK], 1, memory_order_relaxed); atomic_fetch_add_explicit(&p->counters[INSPECTION_DROP], 1, memory_order_relaxed); return 0;
    default: atomic_fetch_add_explicit(&p->counters[BAD_VERDICT], 1, memory_order_relaxed); errno = EPROTO; return -1;
    }
local:
    /* Kernel INPUT profile, not transit inspection, owns local services. */
    atomic_fetch_add_explicit(&p->counters[LOCAL_INPUT], 1, memory_order_relaxed); return 1;
}
static int io_error(struct ffn_packet *p)
{
    if (errno == EAGAIN || errno == ENOBUFS) { atomic_fetch_add_explicit(&p->counters[PRESSURE_DROP], 1, memory_order_relaxed); return 0; }
    if (errno == EIO || errno == ENETDOWN) { atomic_fetch_add_explicit(&p->counters[ADMIN_DROP], 1, memory_order_relaxed); return 0; }
    return -1;
}
static void note_batch(_Atomic uint64_t *largest, uint64_t count)
{
    uint64_t seen = atomic_load_explicit(largest, memory_order_relaxed);
    while (count > seen && !atomic_compare_exchange_weak_explicit(largest, &seen, count, memory_order_relaxed, memory_order_relaxed))
        ;
}
#include "ffn_aggregate_packet.inc"
#include "ffn_fe100_packet.inc"
static int receive_burst(struct ffn_packet *p, struct ffn_lane *l)
{
    int count;
    for (unsigned i = 0; i < BURST; i++) {
        memset(&l->addresses[i], 0, sizeof(l->addresses[i]));
        l->messages[i].msg_hdr.msg_namelen = sizeof(l->addresses[i]);
        l->messages[i].msg_hdr.msg_flags = 0;
    }
    atomic_fetch_add_explicit(&p->receive_calls, 1, memory_order_relaxed);
    count = recvmmsg(l->rx, l->messages, BURST, MSG_DONTWAIT | MSG_TRUNC, NULL);
    if (count < 0) return errno == EAGAIN || errno == EINTR ? 0 : io_error(p);
    atomic_fetch_add_explicit(&p->receive_frames, count, memory_order_relaxed);
    note_batch(&p->receive_max_batch, (uint64_t)count);
    for (int i = 0; i < count; i++) {
        uint8_t *packet = l->frames[i];
        ssize_t n = l->messages[i].msg_len;
        int accepted;
        if (l->addresses[i].sll_family == AF_PACKET && l->addresses[i].sll_pkttype == PACKET_OUTGOING) {
            atomic_fetch_add_explicit(&p->counters[OUTGOING], 1, memory_order_relaxed); continue;
        }
        if ((l->messages[i].msg_hdr.msg_flags & MSG_TRUNC) || (size_t)n > sizeof(l->frames[i])) {
            atomic_fetch_add_explicit(&p->counters[ENVELOPE_DROP], 1, memory_order_relaxed); continue;
        }
        if (fe100_receive(p, packet, &n)) continue;
        if (p->aggregate) {
            if (aggregate_receive(p, l, packet, n)) return -1;
            continue;
        }
        if (n < 18 || n > MAX_FRAME + 4 || be16(packet) != 24 || be16(packet + 2) != p->source) {
            atomic_fetch_add_explicit(&p->counters[ENVELOPE_DROP], 1, memory_order_relaxed); continue;
        }
        /* No carrier as the owner observes it: the frame is not delivered. */
        if (!atomic_load_explicit(&p->carrier, memory_order_acquire)) {
            atomic_fetch_add_explicit(&p->carrier_drops, 1, memory_order_relaxed); continue;
        }
        accepted = allow(p, packet + 4, (size_t)n - 4);
        if (accepted < 0) return -1;
        if (!accepted) continue;
        n -= 4;
        ssize_t written = write(l->tap, packet + 4, (size_t)n);
        if (written < 0) { if (io_error(p)) return -1; continue; }
        if (written != n) { errno = EIO; return -1; }
        atomic_fetch_add_explicit(&p->counters[RX], 1, memory_order_relaxed);
    }
    return 0;
}
static int transmit_batch(struct ffn_packet *p, struct ffn_lane *l, unsigned count)
{
    unsigned offset = 0;
    while (offset < count) {
        atomic_fetch_add_explicit(&p->transmit_calls, 1, memory_order_relaxed);
        int sent = sendmmsg(l->tx, l->tx_messages + offset, count - offset,
                           MSG_DONTWAIT | MSG_NOSIGNAL);
        if (sent < 0) {
            /* Never replay the accepted prefix after a partial send. A busy
             * nonblocking queue drops only the remaining suffix, with exact
             * packet accounting; no unbounded retry can delay control fences. */
            int error = errno;
            if (error == EINTR) {
                atomic_fetch_add_explicit(&p->counters[PRESSURE_DROP], count - offset, memory_order_relaxed);
                return 0;
            }
            if (io_error(p)) return -1;
            unsigned counter = error == EAGAIN || error == ENOBUFS ? PRESSURE_DROP : ADMIN_DROP;
            atomic_fetch_add_explicit(&p->counters[counter], count - offset - 1, memory_order_relaxed);
            return 0;
        }
        if (!sent) { errno = EIO; return -1; }
        for (int i = 0; i < sent; i++)
            if (l->tx_messages[offset + i].msg_len != l->tx_vectors[offset + i].iov_len) {
                errno = EIO; return -1;
            }
        atomic_fetch_add_explicit(&p->counters[TX], sent, memory_order_relaxed);
        atomic_fetch_add_explicit(&p->transmit_frames, sent, memory_order_relaxed);
        note_batch(&p->transmit_max_batch, (uint64_t)sent);
        offset += (unsigned)sent;
    }
    return 0;
}
static int transmit_burst(struct ffn_packet *p, struct ffn_lane *l)
{
    uint8_t frame[MAX_FRAME + 1]; unsigned i, count = 0;
    for (i = 0; i < BURST; i++) {
        ssize_t n = read(l->tap, frame, sizeof(frame));
        if (n < 0) {
            if (errno == EAGAIN || errno == EINTR) break;
            return io_error(p);
        }
        if (!n) { errno = EPIPE; return -1; }
        if (p->aggregate) {
            if (aggregate_transmit(p, l, frame, n)) return -1;
            continue;
        }
        if (n < 14 || n > MAX_FRAME) { atomic_fetch_add_explicit(&p->counters[LENGTH_DROP], 1, memory_order_relaxed); continue; }
        if (n < 60) { memset(frame + n, 0, (size_t)(60 - n)); n = 60; }
        uint8_t *packet = l->tx_frames[count];
        packet[0] = 1; packet[1] = (uint8_t)(p->source >> 8); packet[2] = (uint8_t)p->source; packet[3] = 0;
        memcpy(packet + 4, frame, 12); memset(packet + 16, 0, 8);
        memcpy(packet + 24, frame + 12, (size_t)n - 12);
        l->tx_vectors[count].iov_len = (size_t)n + 12;
        l->tx_messages[count].msg_len = 0;
        count++;
    }
    return transmit_batch(p, l, count);
}
int ffn_packet_poll(struct ffn_packet *p, unsigned budget_ms)
{
    int64_t end, now;
    struct ffn_lane *l;
    if (!p || !budget_ms || budget_ms > 250) { errno = EINVAL; return -1; }
    /* Threaded callers wait for status/control cadence, never process packets. */
    if (p->started) {
        int fault;
        (void)poll(NULL, 0, (int)budget_ms);
        pthread_mutex_lock(&p->control); fault = p->fault;
        pthread_mutex_unlock(&p->control);
        if (fault) { errno = fault; return -1; }
        return 0;
    }
    l = p->lanes[0];
    now = milliseconds(); if (now < 0) return -1;
    end = now + budget_ms;
    do {
        struct pollfd fds[2] = {{l->rx, POLLIN, 0}, {l->tap, POLLIN, 0}};
        int ready = poll(fds, 2, (int)(end - now));
        if (ready < 0) return errno == EINTR ? 0 : -1;
        if (!ready) return 0;
        if ((fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) ||
            (fds[1].revents & (POLLHUP | POLLNVAL))) {
            errno = EPIPE; return -1;
        }
        if ((fds[0].revents & POLLIN) && receive_burst(p, l)) return -1;
        if ((fds[1].revents & POLLIN) && transmit_burst(p, l)) return -1;
        /* Linux TAP reports POLLERR while administratively down. Keep the
         * exclusive attachment through an address/MTU commit, without spinning
         * or inventing packet drops for an empty queue. */
        if ((fds[1].revents & POLLERR) && !(fds[0].revents & POLLIN)) {
            int64_t left = end - milliseconds();
            if (left > 0) (void)poll(NULL, 0, (int)(left > 10 ? 10 : left));
        }
        now = milliseconds(); if (now < 0) return -1;
    } while (now < end);
    return 0;
}
/* Additive: the worker fault, if any, without waiting. A caller that drives
 * many owners from one loop polls its own descriptors and asks each owner this
 * once a tick; ffn_packet_poll sleeps for its budget, which one owner may do
 * and twenty-four may not. 0 while the workers run (or before they start),
 * else the errno the owner's poll would have reported. */
int ffn_packet_fault(struct ffn_packet *p)
{
    int fault;
    if (!p) { errno = EINVAL; return -1; }
    if (!p->started) return 0;
    pthread_mutex_lock(&p->control); fault = p->fault; pthread_mutex_unlock(&p->control);
    return fault;
}
int ffn_packet_counters(struct ffn_packet *p, uint64_t *out, unsigned count)
{
    if (!p || !out || count != FFN_PACKET_COUNTERS) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++) out[i] = atomic_load_explicit(&p->counters[i], memory_order_relaxed);
    return 0;
}

int ffn_packet_receive_stats(struct ffn_packet *p, uint64_t *out, unsigned count)
{
    if (!p || !out || count != 3) { errno = EINVAL; return -1; }
    out[0] = atomic_load_explicit(&p->receive_calls, memory_order_relaxed);
    out[1] = atomic_load_explicit(&p->receive_frames, memory_order_relaxed);
    out[2] = atomic_load_explicit(&p->receive_max_batch, memory_order_relaxed);
    return 0;
}

int ffn_packet_carrier(struct ffn_packet *p, int up)
{
    int value = !!up;
    if (!p) { errno = EINVAL; return -1; }
    /* Every queue of the TAP is the same device; one ioctl sets the carrier. */
    for (unsigned i = 0; i < p->lane_count; i++)
        if (p->lanes[i]->tap >= 0 && ioctl(p->lanes[i]->tap, TUNSETCARRIER, &value)) return -1;
    atomic_store_explicit(&p->carrier, value, memory_order_release);
    return 0;
}

int ffn_packet_carrier_stats(struct ffn_packet *p, uint64_t *out, unsigned count)
{
    if (!p || !out || count != 2) { errno = EINVAL; return -1; }
    out[0] = atomic_load_explicit(&p->carrier_drops, memory_order_relaxed);
    out[1] = (uint64_t)atomic_load_explicit(&p->carrier, memory_order_relaxed);
    return 0;
}

int ffn_packet_transmit_stats(struct ffn_packet *p, uint64_t *out, unsigned count)
{
    if (!p || !out || count != 3) { errno = EINVAL; return -1; }
    out[0] = atomic_load_explicit(&p->transmit_calls, memory_order_relaxed);
    out[1] = atomic_load_explicit(&p->transmit_frames, memory_order_relaxed);
    out[2] = atomic_load_explicit(&p->transmit_max_batch, memory_order_relaxed);
    return 0;
}

static void *packet_worker(void *argument)
{
    typeof(((struct ffn_lane *)0)->arguments[0]) *a = argument;
    struct ffn_packet *p = a->owner;
    struct ffn_lane *l = p->lanes[a->lane];
    unsigned direction = a->direction;
    pthread_mutex_lock(&p->control);
    l->tids[direction] = (int)syscall(SYS_gettid);
    pthread_mutex_unlock(&p->control);
    for (;;) {
        pthread_mutex_lock(&p->control);
        while (p->pause_requested && !p->stopped) {
            l->paused[direction] = 1; pthread_cond_broadcast(&p->changed);
            pthread_cond_wait(&p->changed, &p->control);
        }
        l->paused[direction] = 0;
        if (!p->stopped && milliseconds() >= p->lease_end) {
            p->fault = ETIMEDOUT; p->stopped = 1; pthread_cond_broadcast(&p->changed);
        }
        int stop = p->stopped;
        pthread_mutex_unlock(&p->control);
        if (stop) break;
        struct pollfd fds[2] = {{ direction ? l->tap : l->rx, POLLIN, 0 }, {l->wake[direction], POLLIN, 0}};
        int result = poll(fds, 2, 20), failure = 0;
        struct pollfd fd = fds[0];
        if (fds[1].revents & POLLIN) {
            uint64_t value; ssize_t got;
            do { got = read(l->wake[direction], &value, sizeof(value)); } while (got < 0 && errno == EINTR);
            if (got != sizeof(value) && !(got < 0 && errno == EAGAIN)) {
                pthread_mutex_lock(&p->control); p->fault = EIO; p->stopped = 1;
                pthread_cond_broadcast(&p->changed); pthread_mutex_unlock(&p->control);
            }
            continue;
        }
        if (result < 0 && errno != EINTR) failure = errno;
        else if (fd.revents & (POLLHUP | POLLNVAL)) failure = EPIPE;
        else if ((fd.revents & POLLERR) && !direction) failure = EIO;
        else if (fd.revents & POLLIN) {
            if (direction ? transmit_burst(p, l) : receive_burst(p, l)) failure = errno;
        } else if (fd.revents & POLLERR) (void)poll(NULL, 0, 10);
        if (failure) {
            pthread_mutex_lock(&p->control);
            if (!p->fault) p->fault = failure;
            p->stopped = 1; pthread_cond_broadcast(&p->changed);
            pthread_mutex_unlock(&p->control); break;
        }
    }
    pthread_mutex_lock(&p->control);
    l->paused[direction] = 1; pthread_cond_broadcast(&p->changed);
    pthread_mutex_unlock(&p->control);
    return NULL;
}
int ffn_packet_pause(struct ffn_packet *p)
{
    int fault;
    if (!p) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    p->pause_requested = 1;
    for (unsigned i = 0; i < p->lane_count; i++)
        for (unsigned d = 0; d < p->lanes[i]->started; d++) {
            uint64_t value = 1; ssize_t sent;
            do { sent = write(p->lanes[i]->wake[d], &value, sizeof(value)); } while (sent < 0 && errno == EINTR);
            if (sent != sizeof(value) && !(sent < 0 && errno == EAGAIN)) {
                p->fault = EIO; p->stopped = 1; pthread_cond_broadcast(&p->changed);
            }
        }
    while (p->started == 2 * p->lane_count && !all_paused(p) && !p->stopped)
        pthread_cond_wait(&p->changed, &p->control);
    fault = p->fault;
    pthread_mutex_unlock(&p->control);
    if (fault) { errno = fault; return -1; }
    return 0;
}
int ffn_packet_resume(struct ffn_packet *p)
{
    if (!p) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    if (p->started != 2 * p->lane_count || p->stopped) {
        int error = p->fault ? p->fault : EINVAL;
        pthread_mutex_unlock(&p->control); errno = error; return -1;
    }
    p->lease_end = milliseconds() + LEASE_MS;
    p->pause_requested = 0; pthread_cond_broadcast(&p->changed);
    pthread_mutex_unlock(&p->control); return 0;
}
int ffn_packet_renew(struct ffn_packet *p)
{
    if (!p) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    if (p->started != 2 * p->lane_count || p->stopped) {
        int error = p->fault ? p->fault : EINVAL;
        pthread_mutex_unlock(&p->control); errno = error; return -1;
    }
    p->lease_end = milliseconds() + LEASE_MS;
    pthread_mutex_unlock(&p->control); return 0;
}
/* Start both workers of every lane, each pinned to the CPU given for it:
 * rx_cpus[i] and tx_cpus[i] for lane i. Returns paused. */
int ffn_packet_start_lanes(struct ffn_packet *p, const int *rx_cpus, const int *tx_cpus, unsigned count)
{
    cpu_set_t allowed, affinity;
    pthread_attr_t attr;
    int rc;
    if (!p || p->started || !rx_cpus || !tx_cpus || count != p->lane_count) { errno = EINVAL; return -1; }
    for (unsigned i = 0; i < count; i++)
        if (rx_cpus[i] < 0 || tx_cpus[i] < 0 || rx_cpus[i] >= CPU_SETSIZE || tx_cpus[i] >= CPU_SETSIZE) {
            errno = EINVAL; return -1;
        }
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) return -1;
    for (unsigned i = 0; i < count; i++)
        if (!CPU_ISSET(rx_cpus[i], &allowed) || !CPU_ISSET(tx_cpus[i], &allowed)) { errno = EPERM; return -1; }
    p->pause_requested = 1;
    for (unsigned i = 0; i < count; i++) {
        struct ffn_lane *l = p->lanes[i];
        int cpus[2] = {rx_cpus[i], tx_cpus[i]};
        for (unsigned d = 0; d < 2; d++) {
            l->wake[d] = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
            if (l->wake[d] < 0) { rc = errno; goto fail; }
            rc = pthread_attr_init(&attr); if (rc) goto fail;
            CPU_ZERO(&affinity); CPU_SET(cpus[d], &affinity);
            rc = pthread_attr_setaffinity_np(&attr, sizeof(affinity), &affinity);
            l->cpus[d] = cpus[d]; l->arguments[d].owner = p; l->arguments[d].lane = i; l->arguments[d].direction = d;
            if (!rc) rc = pthread_create(&l->threads[d], &attr, packet_worker, &l->arguments[d]);
            pthread_attr_destroy(&attr);
            if (rc) goto fail;
            l->started++; p->started++;
        }
    }
    return ffn_packet_pause(p);
fail:
    pthread_mutex_lock(&p->control); p->fault = rc; p->stopped = 1;
    pthread_cond_broadcast(&p->changed); pthread_mutex_unlock(&p->control);
    errno = rc; return -1; /* close() joins every successfully created worker. */
}
int ffn_packet_start(struct ffn_packet *p, int rx_cpu, int tx_cpu)
{
    if (!p || p->lane_count != 1) { errno = EINVAL; return -1; }
    return ffn_packet_start_lanes(p, &rx_cpu, &tx_cpu, 1);
}
int ffn_packet_workers(struct ffn_packet *p, int *out, unsigned count)
{
    if (!p || !out || count != 8) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    out[0] = (int)p->started; out[1] = p->lanes[0]->cpus[0]; out[2] = p->lanes[0]->cpus[1];
    out[3] = p->lanes[0]->tids[0]; out[4] = p->lanes[0]->tids[1]; out[5] = p->pause_requested;
    out[6] = p->stopped; out[7] = p->fault;
    pthread_mutex_unlock(&p->control); return 0;
}
/* One lane's workers: started, rx cpu, tx cpu, rx tid, tx tid. */
int ffn_packet_lane_workers(struct ffn_packet *p, unsigned lane, int *out, unsigned count)
{
    struct ffn_lane *l;
    if (!p || !out || count != 5 || lane >= p->lane_count) { errno = EINVAL; return -1; }
    l = p->lanes[lane];
    pthread_mutex_lock(&p->control);
    out[0] = (int)l->started; out[1] = l->cpus[0]; out[2] = l->cpus[1]; out[3] = l->tids[0]; out[4] = l->tids[1];
    pthread_mutex_unlock(&p->control); return 0;
}
