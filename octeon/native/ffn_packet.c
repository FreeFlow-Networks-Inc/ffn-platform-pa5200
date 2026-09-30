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
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_FRAME 1518
#define BURST 64
enum { RX, TX, ENVELOPE_DROP, LENGTH_DROP, PRESSURE_DROP, ADMIN_DROP,
       LOCAL_INPUT, INSPECTION_DROP, BYPASSED, UNSUPPORTED, MALFORMED,
       NO_MATCH, ALERT, BLOCK, OUTGOING, BAD_VERDICT };
struct ffn_packet {
    int rx, tx, tap;
    unsigned source, n4, n6;
    uint8_t local4[FFN_PACKET_LOCAL_MAX][4], local6[FFN_PACKET_LOCAL_MAX][16];
    void *inspection;
    ffn_scan_fn scan;
    uint64_t counters[FFN_PACKET_COUNTERS];
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
    if (p->rx >= 0) close(p->rx);
    if (p->tx >= 0) close(p->tx);
    if (p->tap >= 0) close(p->tap);
    free(p);
}
static struct ffn_packet *allocate(unsigned source)
{
    struct ffn_packet *p;
    if (source > 65535) { errno = EINVAL; return NULL; }
    p = calloc(1, sizeof(*p));
    if (p) { p->rx = p->tx = p->tap = -1; p->source = source; }
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
static int packet_socket(const char *name, unsigned source, int receive)
{
    unsigned index = if_nametoindex(name);
    struct sockaddr_ll addr = { .sll_family = AF_PACKET, .sll_ifindex = (int)index };
    int fd, yes = 1, buffer = 4 * 1024 * 1024, error;
    struct sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_H | BPF_ABS, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, 24, 0, 3),
        BPF_STMT(BPF_LD | BPF_H | BPF_ABS, 2),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, source, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, 65535),
        BPF_STMT(BPF_RET | BPF_K, 0)
    };
    struct sock_fprog program = { .len = 6, .filter = instructions };
    if (!index) { errno = ENODEV; return -1; }
    fd = socket(AF_PACKET, SOCK_RAW | SOCK_CLOEXEC | SOCK_NONBLOCK, receive ? htons(3) : 0);
    if (fd < 0) return -1;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr))) goto fail;
    if (receive && (setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &program, sizeof(program)) ||
        setsockopt(fd, SOL_PACKET, PACKET_IGNORE_OUTGOING, &yes, sizeof(yes)) ||
        setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &buffer, sizeof(buffer)))) goto fail;
    return fd;
fail:
    error = errno; close(fd); errno = error; return -1;
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
    if (n4) memcpy(p->local4, v4, n4 * 4);
    if (n6) memcpy(p->local6, v6, n6 * 16);
    p->n4 = n4; p->n6 = n6; p->inspection = inspection; p->scan = scan;
    return 0;
}
static int allow(struct ffn_packet *p, const uint8_t *frame, size_t size)
{
    unsigned i; int verdict;
    if (size >= 34 && be16(frame + 12) == 0x0800 && frame[14] >> 4 == 4) {
        for (i = 0; i < p->n4; i++) if (!memcmp(frame + 30, p->local4[i], 4)) goto local;
    } else if (size >= 54 && be16(frame + 12) == 0x86dd && frame[14] >> 4 == 6) {
        for (i = 0; i < p->n6; i++) if (!memcmp(frame + 38, p->local6[i], 16)) goto local;
    }
    if (!p->scan) { p->counters[BYPASSED]++; return 1; }
    verdict = p->scan(p->inspection, (const char *)frame, (unsigned)size);
    switch (verdict) {
    case -2: p->counters[UNSUPPORTED]++; return 1;
    case -1: p->counters[MALFORMED]++; return 1;
    case 0: p->counters[NO_MATCH]++; return 1;
    case 1: p->counters[ALERT]++; return 1;
    case 2: p->counters[BLOCK]++; p->counters[INSPECTION_DROP]++; return 0;
    default: p->counters[BAD_VERDICT]++; errno = EPROTO; return -1;
    }
local:
    /* Kernel INPUT profile, not transit inspection, owns local services. */
    p->counters[LOCAL_INPUT]++; return 1;
}
static int io_error(struct ffn_packet *p)
{
    if (errno == EAGAIN || errno == ENOBUFS) { p->counters[PRESSURE_DROP]++; return 0; }
    if (errno == EIO || errno == ENETDOWN) { p->counters[ADMIN_DROP]++; return 0; }
    return -1;
}
static int receive_burst(struct ffn_packet *p)
{
    uint8_t packet[MAX_FRAME + 5]; unsigned i;
    for (i = 0; i < BURST; i++) {
        struct sockaddr_ll addr = {0}; socklen_t addrlen = sizeof(addr);
        ssize_t n = recvfrom(p->rx, packet, sizeof(packet), MSG_TRUNC, (struct sockaddr *)&addr, &addrlen);
        int accepted;
        if (n < 0) return errno == EAGAIN ? 0 : io_error(p);
        if (addr.sll_family == AF_PACKET && addr.sll_pkttype == PACKET_OUTGOING) {
            p->counters[OUTGOING]++; continue;
        }
        if (n < 18 || n > MAX_FRAME + 4 || be16(packet) != 24 || be16(packet + 2) != p->source) {
            p->counters[ENVELOPE_DROP]++; continue;
        }
        accepted = allow(p, packet + 4, (size_t)n - 4);
        if (accepted < 0) return -1;
        if (!accepted) continue;
        n -= 4;
        ssize_t written = write(p->tap, packet + 4, (size_t)n);
        if (written < 0) { if (io_error(p)) return -1; continue; }
        if (written != n) { errno = EIO; return -1; }
        p->counters[RX]++;
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
        if (n < 14 || n > MAX_FRAME) { p->counters[LENGTH_DROP]++; continue; }
        if (n < 60) { memset(frame + n, 0, (size_t)(60 - n)); n = 60; }
        packet[0] = 1; packet[1] = (uint8_t)(p->source >> 8); packet[2] = (uint8_t)p->source; packet[3] = 0;
        memcpy(packet + 4, frame, 12); memset(packet + 16, 0, 8);
        memcpy(packet + 24, frame + 12, (size_t)n - 12);
        ssize_t sent = send(p->tx, packet, (size_t)n + 12, MSG_NOSIGNAL);
        if (sent < 0) { if (io_error(p)) return -1; continue; }
        if (sent != n + 12) { errno = EIO; return -1; }
        p->counters[TX]++;
    }
    return 0;
}
int ffn_packet_poll(struct ffn_packet *p, unsigned budget_ms)
{
    int64_t end, now;
    if (!p || !budget_ms || budget_ms > 250) { errno = EINVAL; return -1; }
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
    memcpy(out, p->counters, sizeof(p->counters)); return 0;
}
