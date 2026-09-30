/* SPDX-License-Identifier: GPL-2.0-or-later */
#define _GNU_SOURCE
#include "ffn_packet.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/if_packet.h>
#include <linux/if_tun.h>
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
struct ffn_packet {
    int rx, tx, tap, wake[2];
    /* Stable heap-backed buffers: no per-packet allocation or large worker stack. */
    struct mmsghdr messages[BURST];
    struct iovec vectors[BURST];
    struct sockaddr_ll addresses[BURST];
    uint8_t frames[BURST][MAX_FRAME + 5];
    _Atomic uint64_t receive_calls, receive_frames, receive_max_batch;
    struct ffn_aggregate *aggregate;
    unsigned source, n4, n6;
    uint8_t local4[FFN_PACKET_LOCAL_MAX][4], local6[FFN_PACKET_LOCAL_MAX][16];
    void *inspection;
    ffn_scan_fn scan;
    _Atomic uint64_t counters[FFN_PACKET_COUNTERS];
    pthread_mutex_t control;
    pthread_cond_t changed;
    pthread_t threads[2];
    struct { struct ffn_packet *owner; unsigned direction; } arguments[2];
    unsigned started;
    int pause_requested, stopped, paused[2], cpus[2], tids[2], fault;
    int64_t lease_end;
};

unsigned ffn_packet_abi(void) { return FFN_PACKET_ABI; }
static uint16_t be16(const uint8_t *p) { return (uint16_t)p[0] << 8 | p[1]; }
static int64_t milliseconds(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
void ffn_packet_close(struct ffn_packet *p)
{
    if (!p) return;
    pthread_mutex_lock(&p->control);
    p->stopped = 1; pthread_cond_broadcast(&p->changed);
    pthread_mutex_unlock(&p->control);
    for (unsigned i = 0; i < p->started; i++) pthread_join(p->threads[i], NULL);
    if (p->rx >= 0) close(p->rx);
    if (p->tx >= 0) close(p->tx);
    if (p->tap >= 0) close(p->tap);
    for (unsigned i = 0; i < 2; i++) if (p->wake[i] >= 0) close(p->wake[i]);
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
        p->rx = p->tx = p->tap = p->wake[0] = p->wake[1] = -1; p->source = source;
        rc = pthread_mutex_init(&p->control, NULL);
        if (rc) { free(p); errno = rc; return NULL; }
        rc = pthread_cond_init(&p->changed, NULL);
        if (rc) { pthread_mutex_destroy(&p->control); free(p); errno = rc; return NULL; }
        for (unsigned i = 0; i < BURST; i++) {
            p->vectors[i] = (struct iovec){p->frames[i], sizeof(p->frames[i])};
            p->messages[i].msg_hdr.msg_iov = &p->vectors[i];
            p->messages[i].msg_hdr.msg_iovlen = 1;
            p->messages[i].msg_hdr.msg_name = &p->addresses[i];
        }
    }
    return p;
}
static int nonblock(int fd)
{
    int flags = fcntl(fd, F_GETFL);
    return flags < 0 ? -1 : fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}
struct ffn_packet *ffn_packet_adopt(int rx, int tx, int tap, unsigned source)
{
    struct ffn_packet *p = allocate(source);
    if (!p) return NULL;
    p->rx = fcntl(rx, F_DUPFD_CLOEXEC, 3);
    p->tx = fcntl(tx, F_DUPFD_CLOEXEC, 3);
    p->tap = fcntl(tap, F_DUPFD_CLOEXEC, 3);
    if (p->rx < 0 || p->tx < 0 || p->tap < 0 || nonblock(p->rx) ||
        nonblock(p->tx) || nonblock(p->tap)) {
        int error = errno; ffn_packet_close(p); errno = error; return NULL;
    }
    return p;
}
static int valid_name(const char *s)
{
    size_t n;
    if (!s || !(n = strlen(s)) || n >= IFNAMSIZ) return 0;
    return strspn(s, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-") == n
           && strcmp(s, ".") && strcmp(s, "..");
}
static int packet_socket_many(const char *name, const uint32_t *sources, unsigned count, int mode)
{
    unsigned index = if_nametoindex(name);
    struct sockaddr_ll addr = { .sll_family = AF_PACKET, .sll_ifindex = (int)index };
    int fd, yes = 1, buffer = 4 * 1024 * 1024, error;
    struct sock_filter instructions[32];
    struct sock_fprog program = { .filter = instructions };
    unsigned used = 0, drop;
    if (count > 16 || (mode && (!sources || !count))) { errno = EINVAL; return -1; }
#define INS(code, jt, jf, k) instructions[used++] = (struct sock_filter){code, jt, jf, k}
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 0);
    INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 0, 24);
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 2);
    for (unsigned i = 0; i < count; i++) {
        if (sources[i] > 65535) { errno = EINVAL; return -1; }
        INS(BPF_JMP | BPF_JEQ | BPF_K, count - i, 0, sources[i]);
    }
    drop = used; INS(BPF_RET | BPF_K, 0, 0, 0);
    instructions[1].jf = (uint8_t)(drop - 2);
    if (mode == 2) { /* Slow protocol control frames only. */
        INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 16);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, 0x8809);
        INS(BPF_RET | BPF_K, 0, 0, 0);
    } else if (mode == 3) { /* No LACP/LLDP copies in the data queue. */
        INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 16);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, 0x8809);
        INS(BPF_JMP | BPF_JEQ | BPF_K, 0, 1, 0x88cc);
        INS(BPF_RET | BPF_K, 0, 0, 0);
    }
    INS(BPF_RET | BPF_K, 0, 0, 65535);
    program.len = (unsigned short)used;
#undef INS
    if (!index) { errno = ENODEV; return -1; }
    fd = socket(AF_PACKET, SOCK_RAW | SOCK_CLOEXEC | SOCK_NONBLOCK, mode ? htons(3) : 0);
    if (fd < 0) return -1;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr))) goto fail;
    if (mode && (setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &program, sizeof(program)) ||
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
struct ffn_packet *ffn_packet_open(const char *wire, const char *namespace,
                                    const char *tap, unsigned source)
{
    struct ffn_packet *p;
    struct ifreq request = {0};
    char path[128];
    int original = -1, target = -1, switched = 0, error;
    if (!valid_name(wire) || !valid_name(namespace) || !valid_name(tap)) {
        errno = EINVAL; return NULL;
    }
    p = allocate(source); if (!p) return NULL;
    p->rx = packet_socket(wire, source, 1);
    if (p->rx < 0) goto fail;
    p->tx = packet_socket(wire, source, 0);
    if (p->tx < 0) goto fail;
    original = open("/proc/self/ns/net", O_RDONLY | O_CLOEXEC);
    snprintf(path, sizeof(path), "/run/netns/%s", namespace);
    target = open(path, O_RDONLY | O_CLOEXEC);
    if (original < 0 || target < 0 || setns(target, CLONE_NEWNET)) goto fail;
    switched = 1;
    if (!if_nametoindex(tap)) { errno = ENODEV; goto fail; }
    p->tap = open("/dev/net/tun", O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (p->tap < 0) goto fail;
    memcpy(request.ifr_name, tap, strlen(tap) + 1);
    request.ifr_flags = IFF_TAP | IFF_NO_PI;
    if (ioctl(p->tap, TUNSETIFF, &request)) goto fail;
    if (setns(original, CLONE_NEWNET)) goto fail;
    close(original); close(target); return p;
fail:
    error = errno;
    if (switched && setns(original, CLONE_NEWNET)) error = errno;
    if (original >= 0) close(original);
    if (target >= 0) close(target);
    ffn_packet_close(p); errno = error; return NULL;
}
int ffn_packet_configure(struct ffn_packet *p, const uint8_t *v4, unsigned n4,
                         const uint8_t *v6, unsigned n6, void *inspection, ffn_scan_fn scan)
{
    if (!p || n4 > FFN_PACKET_LOCAL_MAX || n6 > FFN_PACKET_LOCAL_MAX ||
        (n4 && !v4) || (n6 && !v6) || (!!inspection != !!scan)) {
        errno = EINVAL; return -1;
    }
    pthread_mutex_lock(&p->control);
    if (p->started && (!p->pause_requested || !p->paused[0] || !p->paused[1])) {
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
#include "ffn_aggregate_packet.inc"
static int receive_burst(struct ffn_packet *p)
{
    int count;
    for (unsigned i = 0; i < BURST; i++) {
        memset(&p->addresses[i], 0, sizeof(p->addresses[i]));
        p->messages[i].msg_hdr.msg_namelen = sizeof(p->addresses[i]);
        p->messages[i].msg_hdr.msg_flags = 0;
    }
    atomic_fetch_add_explicit(&p->receive_calls, 1, memory_order_relaxed);
    count = recvmmsg(p->rx, p->messages, BURST, MSG_DONTWAIT | MSG_TRUNC, NULL);
    if (count < 0) return errno == EAGAIN || errno == EINTR ? 0 : io_error(p);
    atomic_fetch_add_explicit(&p->receive_frames, count, memory_order_relaxed);
    /* RX has exactly one writer, including the synchronous test entry point. */
    if ((uint64_t)count > atomic_load_explicit(&p->receive_max_batch, memory_order_relaxed))
        atomic_store_explicit(&p->receive_max_batch, count, memory_order_relaxed);
    for (int i = 0; i < count; i++) {
        uint8_t *packet = p->frames[i];
        ssize_t n = p->messages[i].msg_len;
        int accepted;
        if (p->addresses[i].sll_family == AF_PACKET && p->addresses[i].sll_pkttype == PACKET_OUTGOING) {
            atomic_fetch_add_explicit(&p->counters[OUTGOING], 1, memory_order_relaxed); continue;
        }
        if (p->aggregate) {
            if (aggregate_receive(p, packet, n)) return -1;
            continue;
        }
        if (n < 18 || n > MAX_FRAME + 4 || be16(packet) != 24 || be16(packet + 2) != p->source) {
            atomic_fetch_add_explicit(&p->counters[ENVELOPE_DROP], 1, memory_order_relaxed); continue;
        }
        accepted = allow(p, packet + 4, (size_t)n - 4);
        if (accepted < 0) return -1;
        if (!accepted) continue;
        n -= 4;
        ssize_t written = write(p->tap, packet + 4, (size_t)n);
        if (written < 0) { if (io_error(p)) return -1; continue; }
        if (written != n) { errno = EIO; return -1; }
        atomic_fetch_add_explicit(&p->counters[RX], 1, memory_order_relaxed);
    }
    return 0;
}
static int transmit_burst(struct ffn_packet *p)
{
    uint8_t frame[MAX_FRAME + 1], packet[MAX_FRAME + 12]; unsigned i;
    for (i = 0; i < BURST; i++) {
        ssize_t n = read(p->tap, frame, sizeof(frame));
        if (n < 0) return errno == EAGAIN ? 0 : io_error(p);
        if (!n) { errno = EPIPE; return -1; }
        if (p->aggregate) {
            if (aggregate_transmit(p, frame, n)) return -1;
            continue;
        }
        if (n < 14 || n > MAX_FRAME) { atomic_fetch_add_explicit(&p->counters[LENGTH_DROP], 1, memory_order_relaxed); continue; }
        if (n < 60) { memset(frame + n, 0, (size_t)(60 - n)); n = 60; }
        packet[0] = 1; packet[1] = (uint8_t)(p->source >> 8); packet[2] = (uint8_t)p->source; packet[3] = 0;
        memcpy(packet + 4, frame, 12); memset(packet + 16, 0, 8);
        memcpy(packet + 24, frame + 12, (size_t)n - 12);
        ssize_t sent = send(p->tx, packet, (size_t)n + 12, MSG_NOSIGNAL);
        if (sent < 0) { if (io_error(p)) return -1; continue; }
        if (sent != n + 12) { errno = EIO; return -1; }
        atomic_fetch_add_explicit(&p->counters[TX], 1, memory_order_relaxed);
    }
    return 0;
}
int ffn_packet_poll(struct ffn_packet *p, unsigned budget_ms)
{
    int64_t end, now;
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
    now = milliseconds(); if (now < 0) return -1;
    end = now + budget_ms;
    do {
        struct pollfd fds[2] = {{p->rx, POLLIN, 0}, {p->tap, POLLIN, 0}};
        int ready = poll(fds, 2, (int)(end - now));
        if (ready < 0) return errno == EINTR ? 0 : -1;
        if (!ready) return 0;
        if ((fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) ||
            (fds[1].revents & (POLLHUP | POLLNVAL))) {
            errno = EPIPE; return -1;
        }
        if ((fds[0].revents & POLLIN) && receive_burst(p)) return -1;
        if ((fds[1].revents & POLLIN) && transmit_burst(p)) return -1;
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

static void *packet_worker(void *argument)
{
    typeof(((struct ffn_packet *)0)->arguments[0]) *a = argument;
    struct ffn_packet *p = a->owner;
    unsigned direction = a->direction;
    pthread_mutex_lock(&p->control);
    p->tids[direction] = (int)syscall(SYS_gettid);
    pthread_mutex_unlock(&p->control);
    for (;;) {
        pthread_mutex_lock(&p->control);
        while (p->pause_requested && !p->stopped) {
            p->paused[direction] = 1; pthread_cond_broadcast(&p->changed);
            pthread_cond_wait(&p->changed, &p->control);
        }
        p->paused[direction] = 0;
        if (!p->stopped && milliseconds() >= p->lease_end) {
            p->fault = ETIMEDOUT; p->stopped = 1; pthread_cond_broadcast(&p->changed);
        }
        int stop = p->stopped;
        pthread_mutex_unlock(&p->control);
        if (stop) break;
        struct pollfd fds[2] = {{ direction ? p->tap : p->rx, POLLIN, 0 }, {p->wake[direction], POLLIN, 0}};
        int result = poll(fds, 2, 20), failure = 0;
        struct pollfd fd = fds[0];
        if (fds[1].revents & POLLIN) {
            uint64_t value; ssize_t got;
            do { got = read(p->wake[direction], &value, sizeof(value)); } while (got < 0 && errno == EINTR);
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
            if (direction ? transmit_burst(p) : receive_burst(p)) failure = errno;
        } else if (fd.revents & POLLERR) (void)poll(NULL, 0, 10);
        if (failure) {
            pthread_mutex_lock(&p->control);
            if (!p->fault) p->fault = failure;
            p->stopped = 1; pthread_cond_broadcast(&p->changed);
            pthread_mutex_unlock(&p->control); break;
        }
    }
    pthread_mutex_lock(&p->control);
    p->paused[direction] = 1; pthread_cond_broadcast(&p->changed);
    pthread_mutex_unlock(&p->control);
    return NULL;
}
int ffn_packet_pause(struct ffn_packet *p)
{
    int fault;
    if (!p) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    p->pause_requested = 1;
    for (unsigned i = 0; i < p->started; i++) {
        uint64_t value = 1; ssize_t sent;
        do { sent = write(p->wake[i], &value, sizeof(value)); } while (sent < 0 && errno == EINTR);
        if (sent != sizeof(value) && !(sent < 0 && errno == EAGAIN)) {
            p->fault = EIO; p->stopped = 1; pthread_cond_broadcast(&p->changed);
        }
    }
    while (p->started == 2 && (!p->paused[0] || !p->paused[1]) && !p->stopped)
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
    if (p->started != 2 || p->stopped) {
        int error = p->fault ? p->fault : EINVAL;
        pthread_mutex_unlock(&p->control); errno = error; return -1;
    }
    p->lease_end = milliseconds() + 3000;
    p->pause_requested = 0; pthread_cond_broadcast(&p->changed);
    pthread_mutex_unlock(&p->control); return 0;
}
int ffn_packet_start(struct ffn_packet *p, int rx_cpu, int tx_cpu)
{
    cpu_set_t allowed, affinity;
    pthread_attr_t attr;
    int cpus[2] = {rx_cpu, tx_cpu}, rc;
    if (!p || p->started || rx_cpu < 0 || tx_cpu < 0 || rx_cpu >= CPU_SETSIZE || tx_cpu >= CPU_SETSIZE) {
        errno = EINVAL; return -1;
    }
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) return -1;
    if (!CPU_ISSET(rx_cpu, &allowed) || !CPU_ISSET(tx_cpu, &allowed)) { errno = EPERM; return -1; }
    p->pause_requested = 1;
    for (unsigned i = 0; i < 2; i++) {
        p->wake[i] = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
        if (p->wake[i] < 0) { rc = errno; goto fail; }
        rc = pthread_attr_init(&attr); if (rc) goto fail;
        CPU_ZERO(&affinity); CPU_SET(cpus[i], &affinity);
        rc = pthread_attr_setaffinity_np(&attr, sizeof(affinity), &affinity);
        p->cpus[i] = cpus[i]; p->arguments[i].owner = p; p->arguments[i].direction = i;
        if (!rc) rc = pthread_create(&p->threads[i], &attr, packet_worker, &p->arguments[i]);
        pthread_attr_destroy(&attr);
        if (rc) goto fail;
        p->started++;
    }
    return ffn_packet_pause(p);
fail:
    pthread_mutex_lock(&p->control); p->fault = rc; p->stopped = 1;
    pthread_cond_broadcast(&p->changed); pthread_mutex_unlock(&p->control);
    errno = rc; return -1; /* close() joins any successfully created worker. */
}
int ffn_packet_workers(struct ffn_packet *p, int *out, unsigned count)
{
    if (!p || !out || count != 8) { errno = EINVAL; return -1; }
    pthread_mutex_lock(&p->control);
    out[0] = (int)p->started; out[1] = p->cpus[0]; out[2] = p->cpus[1];
    out[3] = p->tids[0]; out[4] = p->tids[1]; out[5] = p->pause_requested;
    out[6] = p->stopped; out[7] = p->fault;
    pthread_mutex_unlock(&p->control); return 0;
}
