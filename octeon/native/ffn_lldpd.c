/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ffn-lldpd: the dataplane's LLDP agent for front ports.
 *
 * Reads the intent the plane verb writes (/etc/ffn/lldp.json), transmits an
 * LLDPDU per configured port every interval through the OCTEON packet trunk,
 * receives neighbours from the same trunk (control frames carry the BCM
 * source port in their OTMH prefix) and publishes /run/ffn-lldp/neighbors.json
 * and status.json. SIGHUP reloads the intent; SIGTERM announces shutdown
 * (TTL 0) on every transmitting port before exiting.
 *
 * Own code: no lldpd, no SDK. The frame formats are the trunk's documented
 * ones (ffn_dp_packet_transport.py encode/decode_otmh_ssp). */
#include "ffn_lldp.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t reload_requested, stop_requested;
static void on_hup(int sig) { (void)sig; reload_requested = 1; }
static void on_term(int sig) { (void)sig; stop_requested = 1; }

static time_t monotonic(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec; }

static int read_file(const char *path, char **data, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    size_t cap = 8192, used = 0; char *buf = malloc(cap);
    while (buf) {
        size_t got = fread(buf + used, 1, cap - used, f); used += got;
        if (got == 0) break;
        if (used == cap) { char *n = realloc(buf, cap * 2); if (!n) { free(buf); buf = NULL; break; } buf = n; cap *= 2; }
    }
    fclose(f);
    if (!buf) return -1;
    *data = buf; *len = used; return 0;
}

static int write_atomic(const char *path, const char *text)
{
    char temp[512];
    if (snprintf(temp, sizeof temp, "%s.tmp", path) >= (int)sizeof temp) return -1;
    int fd = open(temp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    size_t len = strlen(text);
    for (size_t off = 0; off < len;) { ssize_t w = write(fd, text + off, len - off); if (w <= 0) { close(fd); unlink(temp); return -1; } off += (size_t)w; }
    if (close(fd) || rename(temp, path)) { unlink(temp); return -1; }
    return 0;
}

/* Only LLDP control frames from the configured sources reach this socket. */
static int attach_filter(int fd, const struct lldp_agent *a)
{
    struct sock_filter ins[8 + LLDP_MAX_PORTS];
    unsigned n = 0;
#define INS(code, jt, jf, k) ins[n++] = (struct sock_filter){ code, jt, jf, k }
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 0);
    INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, LLDP_TRUNK_DESTINATION);
    INS(BPF_RET | BPF_K, 0, 0, 0);
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 16);
    INS(BPF_JMP | BPF_JEQ | BPF_K, 1, 0, LLDP_ETHERTYPE);
    INS(BPF_RET | BPF_K, 0, 0, 0);
    INS(BPF_LD | BPF_H | BPF_ABS, 0, 0, 2);
    unsigned count = a->port_count;
    for (unsigned i = 0; i < count; i++) INS(BPF_JMP | BPF_JEQ | BPF_K, (uint8_t)(count - i), 0, a->ports[i].config.source);
    INS(BPF_RET | BPF_K, 0, 0, 0);
    INS(BPF_RET | BPF_K, 0, 0, 65535);
#undef INS
    struct sock_fprog prog = { (unsigned short)n, ins };
    return setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &prog, sizeof prog);
}

static int open_trunk(const char *name)
{
    unsigned index = if_nametoindex(name);
    if (!index) return -1;
    int fd = socket(AF_PACKET, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, htons(ETH_P_ALL));
    if (fd < 0) return -1;
    struct sockaddr_ll addr = { 0 };
    addr.sll_family = AF_PACKET; addr.sll_protocol = htons(ETH_P_ALL); addr.sll_ifindex = (int)index;
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr)) { close(fd); return -1; }
    return fd;
}

struct daemon {
    struct lldp_agent agent;
    char revision[64], configuration[80], intent_error[224];
    const char *intent_path, *run_dir, *trunk;
    int fd;
    bool dirty;
    time_t published;
};

static void load(struct daemon *d)
{
    char *text; size_t len; char err[128];
    d->intent_error[0] = 0;
    if (read_file(d->intent_path, &text, &len)) { snprintf(d->intent_error, sizeof d->intent_error, "intent unreadable: %s", d->intent_path); d->agent.port_count = 0; return; }
    struct lldp_agent next = d->agent;
    if (lldp_load_intent(&next, text, len, d->revision, sizeof d->revision, d->configuration, sizeof d->configuration, err, sizeof err)) {
        snprintf(d->intent_error, sizeof d->intent_error, "intent rejected: %s", err);
        fprintf(stderr, "ffn-lldpd: %s\n", d->intent_error);
    } else {
        d->agent = next;
        fprintf(stderr, "ffn-lldpd: intent revision %s, %u port(s), interval %us hold %u\n", d->revision, d->agent.port_count, d->agent.interval, d->agent.hold);
    }
    free(text);
    if (d->fd >= 0 && attach_filter(d->fd, &d->agent)) fprintf(stderr, "ffn-lldpd: filter attach failed: %s\n", strerror(errno));
    d->dirty = true;
}

static void publish(struct daemon *d, time_t now)
{
    char path[512];
    char *neighbours = lldp_neighbours_json(&d->agent, now);
    char *status = lldp_status_json(&d->agent, now, d->revision, d->configuration, d->fd >= 0);
    if (neighbours && snprintf(path, sizeof path, "%s/neighbors.json", d->run_dir) < (int)sizeof path) write_atomic(path, neighbours);
    if (status && snprintf(path, sizeof path, "%s/status.json", d->run_dir) < (int)sizeof path) {
        /* append the intent error without re-parsing: status is an object; splice before the final brace */
        if (d->intent_error[0]) {
            size_t len = strlen(status);
            char *with = malloc(len + sizeof d->intent_error + 32);
            if (with && len && status[len - 1] == '}') {
                memcpy(with, status, len - 1);
                snprintf(with + len - 1, sizeof d->intent_error + 32, ", \"error\": \"%s\"}", d->intent_error);
                free(status); status = with;
            } else free(with);
        }
        write_atomic(path, status);
    }
    free(neighbours); free(status);
    d->dirty = false; d->published = now;
}

static void transmit(struct daemon *d, struct lldp_port_state *p, uint16_t ttl)
{
    uint8_t pdu[LLDP_MAX_FRAME], frame[LLDP_MAX_FRAME + 32];
    int n = lldp_build(&d->agent.system, &p->config, ttl, pdu, sizeof pdu);
    if (n < 0) return;
    int f = lldp_frame_for_trunk(&p->config, pdu, (size_t)n, frame, sizeof frame);
    if (f < 0) return;
    if (send(d->fd, frame, (size_t)f, 0) == f) p->tx_frames++;
}

static void receive(struct daemon *d, time_t now)
{
    uint8_t frame[LLDP_MAX_FRAME + 64];
    for (int i = 0; i < 64; i++) {
        ssize_t got = recv(d->fd, frame, sizeof frame, 0);
        if (got <= 0) break;
        uint16_t source;
        int off = lldp_frame_from_trunk(frame, (size_t)got, &source);
        if (off < 0) continue;
        struct lldp_port_state *p = lldp_port_by_source(&d->agent, source);
        if (!p || p->config.mode == LLDP_TRANSMIT_ONLY) continue;
        struct lldp_neighbour parsed; unsigned unknown = 0;
        if (lldp_parse(frame + off, (size_t)got - (size_t)off, &parsed, &unknown)) { p->rx_discarded++; continue; }
        p->rx_frames++; p->rx_unknown_tlvs += unknown;
        if (lldp_table_update(p, &parsed, now) == -1) p->rx_discarded++;
        d->dirty = true;
    }
}

int main(int argc, char **argv)
{
    (void)argc; (void)argv;
    struct daemon d = { 0 };
    const char *intent = getenv("FFN_LLDP_INTENT"), *run = getenv("FFN_LLDP_RUN"), *trunk = getenv("FFN_LLDP_TRUNK");
    d.intent_path = intent ? intent : "/etc/ffn/lldp.json";
    d.run_dir = run ? run : "/run/ffn-lldp";
    d.trunk = trunk ? trunk : "ffnpkt0";
    d.fd = -1;
    mkdir(d.run_dir, 0755);
    signal(SIGHUP, on_hup); signal(SIGTERM, on_term); signal(SIGINT, on_term); signal(SIGPIPE, SIG_IGN);
    time_t retry = 0;
    load(&d);
    for (;;) {
        time_t now = monotonic();
        if (d.fd < 0 && now >= retry) {
            d.fd = open_trunk(d.trunk);
            if (d.fd < 0) { fprintf(stderr, "ffn-lldpd: trunk %s unavailable: %s\n", d.trunk, strerror(errno)); retry = now + 5; }
            else { attach_filter(d.fd, &d.agent); fprintf(stderr, "ffn-lldpd: listening on %s\n", d.trunk); d.dirty = true; }
        }
        if (reload_requested) { reload_requested = 0; load(&d); }
        if (stop_requested) break;
        unsigned ttl = d.agent.interval * d.agent.hold; if (ttl > 65535) ttl = 65535;
        time_t next = now + 1;
        for (unsigned i = 0; i < d.agent.port_count; i++) {
            struct lldp_port_state *p = &d.agent.ports[i];
            if (lldp_table_expire(p, now)) d.dirty = true;
            if (d.fd >= 0 && p->config.mode != LLDP_RECEIVE_ONLY && now >= p->next_tx) { transmit(&d, p, (uint16_t)ttl); p->next_tx = now + (time_t)d.agent.interval; d.dirty = true; }
            if (p->next_tx < next && p->config.mode != LLDP_RECEIVE_ONLY) next = p->next_tx;
        }
        if (d.dirty || now - d.published >= 10) publish(&d, now);
        struct pollfd pfd = { d.fd, POLLIN, 0 };
        int wait_ms = (int)((next > now ? next - now : 0) * 1000);
        if (wait_ms > 1000) wait_ms = 1000;
        int ready = poll(d.fd >= 0 ? &pfd : NULL, d.fd >= 0 ? 1 : 0, wait_ms);
        if (ready > 0 && (pfd.revents & POLLIN)) receive(&d, monotonic());
        if (ready > 0 && (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))) { close(d.fd); d.fd = -1; retry = monotonic() + 2; }
    }
    if (d.fd >= 0) {
        for (unsigned i = 0; i < d.agent.port_count; i++)
            if (d.agent.ports[i].config.mode != LLDP_RECEIVE_ONLY) transmit(&d, &d.agent.ports[i], 0);
        close(d.fd); d.fd = -1;
    }
    publish(&d, monotonic());
    fprintf(stderr, "ffn-lldpd: stopped\n");
    return 0;
}
