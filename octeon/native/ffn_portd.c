// SPDX-License-Identifier: GPL-2.0-or-later
/* ffn-portd: the dataplane owner of a faceplate port or an aggregate.
 *
 * `aggregate`: one process per aeN, described below. `physical`: one process
 * per front port (ethernet1/N, copper and optical alike) owning its TAP's
 * native workers and the carrier the CP observes for it. Both kinds share the
 * helpers, the native library and the lifetime rules in this file.
 *
 * The aggregate owner (formerly ffn-aggregated):
 *
 * One process owns the LACP engine, the member gates, the trunk control
 * socket and the aggregate TAP's native packet workers, and talks to the MP
 * supervisor over the JSON-line contract ffn_aggregate_runtime.py serve had:
 *
 *   stdin   line 1: the owner intent; then {token, sequence, age_seconds,
 *           links:[{port, up, speed_mbps}], offload, config?} every half second
 *   stdout  one status row per second (the first within the MP's startup
 *           budget), or a final {"error": ...} row when the owner stops on a
 *           fault
 *
 * Its life is bound to that pipe: EOF, a missing observation for eight
 * seconds, SIGTERM or the death of its parent all withdraw the gates and
 * release every lock within seconds. The kernel interface transactions
 * (TAP, nftables guard, addresses, VLAN units, DHCP hooks) stay with the
 * Python helpers in ffn_aggregate_runtime.py, invoked as bounded one-shot
 * processes: validate, network-setup, network-update, network-teardown.
 *
 * Replaces the serve loop of ffn_aggregate_runtime.py (platform issue #86). */
#define _GNU_SOURCE
#include "ffn_json.h"
#include "ffn_lacp.h"
#include "ffn_packet.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SECOND 1000000ULL
#define MAX_FRAME 1518
#define MEMBERS 8
#define UNITS 65
#define PATH 256
#define PORTS 24          /* faceplate ports the daemon owns from boot */
#define GROUPS 12         /* ae1..ae12 */
#define CLIENTS 64        /* control socket connections at once */
#define RETRY_ATTACH (2 * SECOND)
#define PUBLISH_PERIOD SECOND
#define LEASE_BEFORE_FIRST (60 * SECOND)
#define LEASE_AFTER_FIRST (8 * SECOND)
#define OBSERVATION_LEASE (6 * SECOND)
#define WORKER_TIMEOUT (30 * SECOND)
#define RETIRED_GRACE (2 * SECOND)
#define STATUS_PERIOD SECOND
#define LLDP_PERIOD (30 * SECOND)
#define POLL_MS 50
#define SHUTDOWN_BUDGET 60
#define LOCK_WAIT (15 * SECOND)
/* Seconds without a status before the hosting daemon counts as hung rather than
 * busy (FFN_PORTD_DAEMON_STALE overrides it; the harness shortens it). */
static double DAEMON_STALE = 60.0;

/* Every packet path this process hosts has a lease of a few seconds that its
 * owner renews from the control loop; the workers stop when it lapses, so a
 * dead owner forwards nothing. The loop is single-threaded and some of its
 * steps wait: an attach waits for the network lock that the network owner and
 * its helpers hold for seconds at a time, a close waits for the kernel to
 * retire the packet sockets, a helper runs for seconds. Each of those waits
 * renews every hosted path, or the ports already serving would fault
 * ("native owner fault: Connection timed out") while a new one attaches, as
 * they did on 2026-10-21 when the DP first rooted over NFS and every helper
 * took longer. */
static void leases_renew(void);

/* Commissioned optical wiring: front port 5..24 -> BCM source system port
 * (ffn_dp_packet_transport.FRONT). */
static const uint16_t FRONT_SOURCE[20] = { 16, 1, 18, 19, 6, 21, 22, 23, 7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35 };
static int front_source(unsigned port) { return port >= 5 && port <= 24 ? FRONT_SOURCE[port - 5] : -1; }
static int front_port(unsigned source)
{
    for (unsigned i = 0; i < 20; i++) if (FRONT_SOURCE[i] == source) return (int)i + 5;
    return -1;
}

static const char *RUN = "/run", *NS = "ffn-data", *TRUNK = "ffnpkt0";
static const char *PYTHON = "/usr/bin/python3", *RUNTIME = "/usr/local/sbin/ffn_aggregate_runtime.py";
static const char *DHCP_HOOK = "/usr/local/sbin/ffn_aggregate_dhcp.py", *STEERING = "/usr/local/sbin/ffn_tap_steering.py";
static const char *CPUS = "/usr/local/sbin/ffn_packet_cpus.py", *CPU_RESERVATIONS = "/run/ffn-packet-cpus";
static const char *INSPECTION_POLICY = "/etc/ffn/inspection.json", *INLINE_LIBRARY = "/usr/local/lib/libffn-inline.so";
static const char *FE100_RETURN = "/etc/ffn/fe100-return.json", *LANES_POLICY = "/etc/ffn/packet-lanes.json";
static const char *NETWORK_JOURNAL = "/etc/ffn/network.json";   /* the network owner's applied intent: revision, ports, routes */
static const char *NETWORK_OWNER = "/usr/local/sbin/ffn_network.py";   /* the network owner itself: `reconcile --port pN` remakes a plain TAP */
static const char *ROUTE_LINKS = "/run/ffn-route-links.json";    /* the route tracker's hardware link observation (see links_tool) */
static const char *SOCKET_NAME = "ffn-portd.sock", *TABLE_NAME = "ffn-portd-table.json", *DAEMON_STATUS = "ffn-portd.json";
static bool unchecked;

static volatile sig_atomic_t stop_signal;
static bool shutting_down;   /* helpers run to completion during the owner's own cleanup */
static void on_stop(int sig) { stop_signal = sig; }

/* --------------------------------------------------------------- utilities */

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * SECOND + (uint64_t)ts.tv_nsec / 1000;
}

static void logmsg(const char *fmt, ...)
{
    va_list ap;
    fputs("ffn-portd: ", stderr);
    va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
    fputc('\n', stderr); fflush(stderr);
}

static int read_file(const char *path, char **data, size_t *len)
{
    FILE *f = fopen(path, "rb");
    size_t cap = 8192, used = 0;
    char *buf;
    if (!f) return -1;
    buf = malloc(cap);
    while (buf) {
        size_t got = fread(buf + used, 1, cap - used, f);
        used += got;
        if (got == 0) break;
        if (used == cap) { char *n = realloc(buf, cap * 2); if (!n) { free(buf); buf = NULL; break; } buf = n; cap *= 2; }
    }
    fclose(f);
    if (!buf) return -1;
    buf[used] = 0;
    *data = buf; *len = used;
    return 0;
}

static int write_atomic(const char *path, const char *text)
{
    char temp[PATH + 8];
    int fd;
    size_t len = strlen(text);
    if (snprintf(temp, sizeof temp, "%s.tmp", path) >= (int)sizeof temp) return -1;
    fd = open(temp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    for (size_t off = 0; off < len;) {
        ssize_t w = write(fd, text + off, len - off);
        if (w <= 0) { close(fd); unlink(temp); return -1; }
        off += (size_t)w;
    }
    if (fchmod(fd, 0600) || close(fd) || rename(temp, path)) { unlink(temp); return -1; }
    return 0;
}

/* NULL with errno ENOENT when the file is absent: callers decide by reading,
 * never by checking first and reading afterwards. */
static struct ffn_json *read_json(const char *path)
{
    char *text; size_t len; char err[64];
    struct ffn_json *doc;
    if (read_file(path, &text, &len)) return NULL;
    doc = ffn_json_parse(text, len, err, sizeof err);
    free(text);
    if (!doc) errno = EINVAL;
    return doc;
}

static int write_json(const char *path, const struct ffn_json *doc)
{
    char *text = ffn_json_dump(doc);
    int rc = text ? write_atomic(path, text) : -1;
    free(text);
    return rc;
}

static void trim(char *s)
{
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1])) s[--n] = 0;
}

static int boot_id(char *out, size_t outsz)
{
    char *text; size_t len;
    if (read_file("/proc/sys/kernel/random/boot_id", &text, &len)) return -1;
    trim(text);
    snprintf(out, outsz, "%s", text);
    free(text);
    return 0;
}

/* The 22nd field of /proc/<pid>/stat: what the Python owners record and compare. */
static int process_start(pid_t pid, char *out, size_t outsz)
{
    char path[64], *text, *p; size_t len;
    snprintf(path, sizeof path, "/proc/%d/stat", (int)pid);
    if (read_file(path, &text, &len)) return -1;
    p = strrchr(text, ')');
    if (!p) { free(text); return -1; }
    p += 2;
    for (int field = 0; field < 19 && p; field++) { p = strchr(p, ' '); if (p) p++; }
    if (!p) { free(text); return -1; }
    snprintf(out, outsz, "%.*s", (int)strcspn(p, " "), p);
    free(text);
    return 0;
}

static bool uuid_text(const char *s)
{
    if (!s || strlen(s) != 36) return false;
    for (unsigned i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) { if (s[i] != '-') return false; }
        else if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
    }
    return true;
}

static int parse_mac(const char *text, uint8_t out[6])
{
    unsigned v[6];
    char tail;
    if (!text || strlen(text) != 17) return -1;
    if (sscanf(text, "%2x:%2x:%2x:%2x:%2x:%2x%c", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &tail) != 6) return -1;
    for (unsigned i = 0; i < 6; i++) {
        if (!isxdigit((unsigned char)text[i * 3]) || !isxdigit((unsigned char)text[i * 3 + 1])) return -1;
        out[i] = (uint8_t)v[i];
    }
    return 0;
}

static void mac_text(const uint8_t mac[6], char out[18])
{
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static uint16_t be16(const uint8_t *p) { return (uint16_t)(p[0] << 8 | p[1]); }

/* ----------------------------------------------------------------- SHA-256 */

struct sha256 { uint32_t h[8]; uint64_t len; uint8_t block[64]; unsigned used; };

static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2 };

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(struct sha256 *s, const uint8_t *p)
{
    uint32_t w[64], a, b, c, d, e, f, g, h;
    for (unsigned i = 0; i < 16; i++) w[i] = (uint32_t)p[i * 4] << 24 | (uint32_t)p[i * 4 + 1] << 16 | (uint32_t)p[i * 4 + 2] << 8 | p[i * 4 + 3];
    for (unsigned i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3]; e = s->h[4]; f = s->h[5]; g = s->h[6]; h = s->h[7];
    for (unsigned i = 0; i < 64; i++) {
        uint32_t S1 = ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25), ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + S1 + ch + K256[i] + w[i];
        uint32_t S0 = ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22), maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

static void sha256_init(struct sha256 *s)
{
    static const uint32_t init[8] = { 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19 };
    memcpy(s->h, init, sizeof init); s->len = 0; s->used = 0;
}

static void sha256_update(struct sha256 *s, const void *data, size_t len)
{
    const uint8_t *p = data;
    s->len += len;
    while (len) {
        size_t take = 64 - s->used; if (take > len) take = len;
        memcpy(s->block + s->used, p, take); s->used += (unsigned)take; p += take; len -= take;
        if (s->used == 64) { sha256_block(s, s->block); s->used = 0; }
    }
}

static void sha256_hex(struct sha256 *s, char out[65])
{
    uint64_t bits = s->len * 8;
    uint8_t pad = 0x80, zero = 0, length[8];
    sha256_update(s, &pad, 1);
    while (s->used != 56) sha256_update(s, &zero, 1);
    for (unsigned i = 0; i < 8; i++) length[i] = (uint8_t)(bits >> (56 - i * 8));
    sha256_update(s, length, 8);
    for (unsigned i = 0; i < 8; i++) snprintf(out + i * 8, 9, "%08x", s->h[i]);
}

/* ---------------------------------------------- Python-canonical JSON text */
/* json.dumps(value, sort_keys=True): the MP hashes the same text to decide
 * whether this owner has acknowledged its network configuration. */

struct text { char *s; size_t len, cap; };

static void text_put(struct text *t, const char *s, size_t n)
{
    if (t->len + n + 1 > t->cap) {
        size_t cap = t->cap ? t->cap * 2 : 1024;
        while (cap < t->len + n + 1) cap *= 2;
        char *p = realloc(t->s, cap);
        if (!p) return;
        t->s = p; t->cap = cap;
    }
    memcpy(t->s + t->len, s, n); t->len += n; t->s[t->len] = 0;
}

static void text_str(struct text *t, const char *s) { text_put(t, s, strlen(s)); }

static void canonical_string(struct text *t, const char *s)
{
    char buf[16];
    text_put(t, "\"", 1);
    while (*s) {
        unsigned char c = (unsigned char)*s;
        uint32_t cp; unsigned extra = 0;
        if (c == '"') { text_put(t, "\\\"", 2); s++; continue; }
        if (c == '\\') { text_put(t, "\\\\", 2); s++; continue; }
        if (c == '\n') { text_put(t, "\\n", 2); s++; continue; }
        if (c == '\r') { text_put(t, "\\r", 2); s++; continue; }
        if (c == '\t') { text_put(t, "\\t", 2); s++; continue; }
        if (c == '\b') { text_put(t, "\\b", 2); s++; continue; }
        if (c == '\f') { text_put(t, "\\f", 2); s++; continue; }
        if (c < 0x20) { snprintf(buf, sizeof buf, "\\u%04x", c); text_str(t, buf); s++; continue; }
        if (c < 0x80) { text_put(t, s, 1); s++; continue; }
        if ((c & 0xe0) == 0xc0) { cp = c & 0x1f; extra = 1; }
        else if ((c & 0xf0) == 0xe0) { cp = c & 0x0f; extra = 2; }
        else if ((c & 0xf8) == 0xf0) { cp = c & 0x07; extra = 3; }
        else { s++; continue; }
        s++;
        for (unsigned i = 0; i < extra; i++, s++) {
            if ((((unsigned char)*s) & 0xc0) != 0x80) break;
            cp = cp << 6 | (((unsigned char)*s) & 0x3f);
        }
        if (cp >= 0x10000) {
            cp -= 0x10000;
            snprintf(buf, sizeof buf, "\\u%04x\\u%04x", 0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
        } else snprintf(buf, sizeof buf, "\\u%04x", cp);
        text_str(t, buf);
    }
    text_put(t, "\"", 1);
}

static int key_compare(const void *a, const void *b) { return strcmp(*(const char *const *)a, *(const char *const *)b); }

static void canonical(struct text *t, const struct ffn_json *n)
{
    char num[64];
    switch (n->type) {
    case FFN_JSON_NULL: text_str(t, "null"); break;
    case FFN_JSON_BOOL: text_str(t, n->boolean ? "true" : "false"); break;
    case FFN_JSON_INT: snprintf(num, sizeof num, "%lld", n->integer); text_str(t, num); break;
    case FFN_JSON_NUMBER: ffn_json_number_text(n->number, num, sizeof num); text_str(t, num); break;
    case FFN_JSON_STRING: canonical_string(t, n->string ? n->string : ""); break;
    case FFN_JSON_ARRAY:
        text_put(t, "[", 1);
        for (size_t i = 0; i < n->count; i++) { if (i) text_put(t, ", ", 2); canonical(t, n->items[i]); }
        text_put(t, "]", 1);
        break;
    case FFN_JSON_OBJECT: {
        const char **keys = calloc(n->count ? n->count : 1, sizeof *keys);
        if (!keys) return;
        for (size_t i = 0; i < n->count; i++) keys[i] = n->keys[i];
        qsort(keys, n->count, sizeof *keys, key_compare);
        text_put(t, "{", 1);
        for (size_t i = 0; i < n->count; i++) {
            if (i) text_put(t, ", ", 2);
            canonical_string(t, keys[i]); text_put(t, ": ", 2);
            canonical(t, ffn_json_get(n, keys[i]));
        }
        text_put(t, "}", 1);
        free(keys);
        break;
    }
    }
}

/* sha256(json.dumps({"lldp": lldp, "network": network}, sort_keys=True)) */
static void network_revision(const struct ffn_json *network, bool lldp, char out[65])
{
    struct text t = { 0 };
    struct sha256 s;
    struct ffn_json *doc = ffn_json_object();
    ffn_json_set(doc, "lldp", ffn_json_bool(lldp));
    ffn_json_set(doc, "network", ffn_json_copy(network));
    canonical(&t, doc);
    ffn_json_free(doc);
    sha256_init(&s);
    sha256_update(&s, t.s ? t.s : "", t.len);
    sha256_hex(&s, out);
    free(t.s);
}

/* ---------------------------------------------------------------- helpers */
/* Bounded child processes: JSON in on stdin, JSON out on stdout, stderr kept
 * for the error report. The helper's own stdin is never this daemon's. */

struct helper {
    pid_t pid;
    int in, out, err;
    struct text input, output, error;
    size_t written;
    uint64_t started;
    bool running;
};

static int helper_start(struct helper *h, char *const argv[], const char *input, char *const envp[])
{
    int in[2], out[2], err[2];
    memset(h, 0, sizeof *h);
    if (pipe2(in, O_CLOEXEC) || pipe2(out, O_CLOEXEC) || pipe2(err, O_CLOEXEC)) return -1;
    h->pid = fork();
    if (h->pid < 0) { close(in[0]); close(in[1]); close(out[0]); close(out[1]); close(err[0]); close(err[1]); return -1; }
    if (h->pid == 0) {
        dup2(in[0], 0); dup2(out[1], 1); dup2(err[1], 2);
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        setsid();
        if (envp) execve(argv[0], argv, envp); else execv(argv[0], argv);
        _exit(127);
    }
    close(in[0]); close(out[1]); close(err[1]);
    h->in = in[1]; h->out = out[0]; h->err = err[0];
    fcntl(h->in, F_SETFL, O_NONBLOCK); fcntl(h->out, F_SETFL, O_NONBLOCK); fcntl(h->err, F_SETFL, O_NONBLOCK);
    if (input) text_str(&h->input, input);
    h->started = now_us(); h->running = true;
    return 0;
}

/* Services the pipes without blocking; true once every pipe is closed. */
static bool helper_service(struct helper *h)
{
    char buf[4096];
    ssize_t n;
    if (h->in >= 0) {
        if (h->written < h->input.len) {
            n = write(h->in, h->input.s + h->written, h->input.len - h->written);
            if (n > 0) h->written += (size_t)n;
            else if (n < 0 && errno != EAGAIN && errno != EINTR) { close(h->in); h->in = -1; }
        }
        if (h->in >= 0 && h->written >= h->input.len) { close(h->in); h->in = -1; }
    }
    if (h->out >= 0) {
        while ((n = read(h->out, buf, sizeof buf)) > 0) if (h->output.len < 1 << 20) text_put(&h->output, buf, (size_t)n);
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(h->out); h->out = -1; }
    }
    if (h->err >= 0) {
        while ((n = read(h->err, buf, sizeof buf)) > 0) if (h->error.len < 1 << 16) text_put(&h->error, buf, (size_t)n);
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(h->err); h->err = -1; }
    }
    return h->in < 0 && h->out < 0 && h->err < 0;
}

/* The exit status once the child has gone; -1 while it runs. */
static int helper_status(struct helper *h)
{
    int status;
    if (!h->running) return -1;
    if (waitpid(h->pid, &status, WNOHANG) != h->pid) return -1;
    h->running = false;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
}

static void helper_kill(struct helper *h)
{
    if (h->running) { kill(-h->pid, SIGKILL); kill(h->pid, SIGKILL); waitpid(h->pid, NULL, 0); h->running = false; }
}

static void helper_close(struct helper *h)
{
    if (h->in >= 0) close(h->in);
    if (h->out >= 0) close(h->out);
    if (h->err >= 0) close(h->err);
    h->in = h->out = h->err = -1;
    free(h->input.s); free(h->output.s); free(h->error.s);
    memset(&h->input, 0, sizeof h->input); memset(&h->output, 0, sizeof h->output); memset(&h->error, 0, sizeof h->error);
}

/* Run to completion within timeout_us. Returns the exit status, or -1 on a
 * timeout (the child is killed). Output and error text stay in h. */
static int helper_run(struct helper *h, char *const argv[], const char *input, uint64_t timeout_us)
{
    int status;
    if (helper_start(h, argv, input, NULL)) return -1;
    for (;;) {
        struct pollfd fds[3]; unsigned n = 0;
        bool closed = helper_service(h);
        status = helper_status(h);
        if (closed && status >= 0) return status;
        if (now_us() - h->started > timeout_us || (stop_signal && !shutting_down)) { helper_kill(h); return -1; }
        if (h->in >= 0 && h->written < h->input.len) fds[n++] = (struct pollfd){ h->in, POLLOUT, 0 };
        if (h->out >= 0) fds[n++] = (struct pollfd){ h->out, POLLIN, 0 };
        if (h->err >= 0) fds[n++] = (struct pollfd){ h->err, POLLIN, 0 };
        leases_renew();
        poll(fds, n, 100);
    }
}

static void last_line(const struct text *t, char *out, size_t outsz)
{
    size_t end = t->len, start;
    while (end && (t->s[end - 1] == '\n' || t->s[end - 1] == '\r')) end--;
    start = end;
    while (start && t->s[start - 1] != '\n') start--;
    snprintf(out, outsz, "%.*s", (int)(end - start), t->s ? t->s + start : "");
}

/* A finished helper's reply: the parsed JSON object, or NULL with err from its
 * error line, its exit status, or its timeout (status -1). */
static struct ffn_json *helper_reply(struct helper *h, int status, const char *verb, char *err, size_t errsz)
{
    struct ffn_json *reply = NULL;
    char perr[64], line[256];
    if (status == 0 && h->output.len) reply = ffn_json_parse(h->output.s, h->output.len, perr, sizeof perr);
    if (!reply || reply->type != FFN_JSON_OBJECT || ffn_json_get(reply, "error")) {
        const char *message = reply ? ffn_json_get_string(reply, "error") : NULL;
        last_line(&h->error, line, sizeof line);
        if (status < 0) snprintf(err, errsz, "%s helper timed out", verb);
        else snprintf(err, errsz, "%s", message && message[0] ? message : line[0] ? line : status ? "helper failed" : "helper reply unreadable");
        ffn_json_free(reply); reply = NULL;
    }
    return reply;
}

/* A runtime verb: {PYTHON} {RUNTIME} verb, JSON in, JSON out. Returns the parsed
 * reply, or NULL with err set from the helper's stderr tail / exit status. */
static struct ffn_json *runtime_verb(const char *verb, const struct ffn_json *request, uint64_t timeout_us, char *err, size_t errsz)
{
    struct helper h;
    char *input = ffn_json_dump(request);
    char *const argv[] = { (char *)PYTHON, (char *)RUNTIME, (char *)verb, NULL };
    struct ffn_json *reply;
    int status;
    if (!input) { snprintf(err, errsz, "out of memory"); return NULL; }
    status = helper_run(&h, argv, input, timeout_us);
    free(input);
    reply = helper_reply(&h, status, verb, err, errsz);
    helper_close(&h);
    return reply;
}

/* Start a runtime verb without waiting for it; the caller services the helper. */
static int runtime_begin(struct helper *h, const char *verb, const struct ffn_json *request)
{
    char *input = ffn_json_dump(request);
    char *const argv[] = { (char *)PYTHON, (char *)RUNTIME, (char *)verb, NULL };
    int rc;
    if (!input) return -1;
    rc = helper_start(h, argv, input, NULL);
    free(input);
    return rc;
}

/* --------------------------------------------------------------- the owner */

struct intent {
    struct ffn_json *doc;        /* the whole intent, kept for the intent file and status */
    char group[8], token[40], boot_id[40], system_text[18];
    unsigned tid;
    uint8_t system[6];
    unsigned count;
    uint16_t members[MEMBERS];   /* intent order: the engine's order */
    uint16_t sorted[MEMBERS];    /* ascending: the native owner's order */
    bool active, fast, lldp, control_only, offload;
    unsigned min_links, priority;
};

struct offload {
    bool enabled;
    uint32_t alias[MEMBERS];     /* by sorted member index */
    unsigned members[MEMBERS], member_count; /* acknowledged hardware members (sorted ports) */
    bool has_deadline;
    uint64_t deadline;
    uint64_t transmitted;
    struct ffn_json *key_program;
};

struct inspector {
    void *lib, *handle;
    void *(*create)(const char *, unsigned, int);
    void *(*create_profile)(const char *, unsigned, int, unsigned);
    void (*destroy)(void *);
    void *scan;
    struct ffn_json *cfg;        /* the loaded policy (DEFAULT when absent) */
    char error[256];
    bool has_error;
    uint64_t next_poll;
    char status_path[PATH];
    struct ffn_json *counts;     /* verdict counters the status file reports */
};

struct returns { bool active; struct ffn_json *rows; uint64_t deadline_ms; };

struct daemon {
    struct intent in;
    char status_path[PATH], intent_path[PATH], lease_path[PATH], pending_path[PATH], dhcp_pid_path[PATH];
    int locks[2 + MEMBERS]; unsigned lock_count;
    char process_start[32];
    /* LACP */
    struct ffn_lacp *engine;
    struct ffn_lacp_gate gate[MEMBERS];  /* by engine member index, as applied through the driver */
    bool gate_valid;
    int control_fd;
    struct ffn_aggregate_member native_members[MEMBERS];  /* sorted */
    struct ffn_packet_fe100_binding control_bindings[MEMBERS]; unsigned control_binding_count; uint64_t control_deadline_ms;
    /* native data path */
    struct ffn_packet *native;
    unsigned lanes, lanes_wanted;   /* lanes running, and what the policy asked for this device */
    char cpu_token[40];
    struct ffn_aggregate_unit rows[UNITS]; unsigned row_count; bool rows_applied;
    char unit_name[4097][24];
    struct { unsigned collect, distribute, inspect, ready; uint64_t deadline, offload_deadline; void *engine; } gates_last; bool gates_pushed;
    struct returns returns, control_returns;
    uint64_t next_fe100;
    uint16_t fronts[MEMBERS]; unsigned front_count;   /* the front ports this owner binds returns for */
    /* network configuration */
    struct ffn_json *network;    /* the applied network (a copy of the intent's, then updates) */
    struct ffn_json *requested;  /* {revision, network, lldp} the MP asked for last */
    char applied_revision[65], attempted_revision[65], lease_generation[65];
    char network_error[512]; bool has_network_error;
    struct helper worker; bool worker_running; struct ffn_json *worker_intent;
    struct ffn_json *unit_status; /* subinterfaces from the last setup/update ack */
    uint8_t local[32][16]; unsigned local_len[32]; unsigned local_count;   /* parent addresses, packed */
    bool created;
    /* DHCP */
    pid_t dhcp; bool dhcp_exited;
    struct { pid_t pid; uint64_t at; } retired[MEMBERS]; unsigned retired_count;
    struct ffn_json *lease;
    /* offload, inspection */
    struct offload off;
    struct inspector insp;
    /* loop */
    uint64_t started, last_input, next_status, next_lldp;
    long sequence;
    struct text stdin_buffer;
    bool stdin_closed;
    char fatal[512]; bool failed;
    /* counters for the status row */
    uint64_t counters[FFN_PACKET_COUNTERS], agg_stats[8], member_rx[MEMBERS], member_tx[MEMBERS];
    struct ffn_json *unit_counts, *workers;
};

static const char *COUNTER_NAMES[FFN_PACKET_COUNTERS] = { "rx", "tx", "envelope_rejected", "length_drop", "backpressure_drop",
    "admin_down_drop", "local_input", "inspection_drop", "bypassed", "unsupported_pass", "malformed_pass", "no_match",
    "alert", "block", "outgoing_ignored", "invalid_verdict" };
static const char *AGGREGATE_STAT_NAMES[8] = { "gate_drops", "unconfigured_or_invalid_vlan_drop", "network_update_drop", "offload_tx",
    "rx_queue_drop", "tx_queue_drop", "control_frame_ignored", "inspection_drop" };
static const char *VERDICTS[6] = { "bypassed", "unsupported_pass", "malformed_pass", "no_match", "alert", "block" };

static void fail(struct daemon *d, const char *fmt, ...)
{
    va_list ap;
    if (d->failed) return;
    d->failed = true;
    va_start(ap, fmt); vsnprintf(d->fatal, sizeof d->fatal, fmt, ap); va_end(ap);
    logmsg("stopping: %s", d->fatal);
}

static int sorted_index(const struct daemon *d, unsigned port)
{
    for (unsigned i = 0; i < d->in.count; i++) if (d->in.sorted[i] == port) return (int)i;
    return -1;
}

/* ------------------------------------------------------------------ intent */

static int compare_ports(const void *a, const void *b) { return (int)*(const uint16_t *)a - (int)*(const uint16_t *)b; }

static int parse_intent(struct daemon *d, const char *text, size_t len, char *err, size_t errsz)
{
    static const char *expected[] = { "group", "token", "boot_id", "system", "members", "lacp", "network", "lldp", "control_only", "offload" };
    struct intent *in = &d->in;
    struct ffn_json *doc, *members, *lacp, *network;
    char perr[96], current_boot[40];
    const char *s;
    long long value;
    bool flag;
    doc = ffn_json_parse(text, len, perr, sizeof perr);
    if (!doc || doc->type != FFN_JSON_OBJECT) { snprintf(err, errsz, "Invalid aggregate owner intent"); ffn_json_free(doc); return -1; }
    if (doc->count != 10) goto invalid;
    for (unsigned i = 0; i < 10; i++) if (!ffn_json_get(doc, expected[i])) goto invalid;
    s = ffn_json_get_string(doc, "group");
    if (!s || strncmp(s, "ae", 2) || strlen(s) > 4 || strlen(s) < 3) { snprintf(err, errsz, "Invalid aggregate name"); goto out; }
    in->tid = (unsigned)atoi(s + 2);
    if (in->tid < 1 || in->tid > 12 || (s[2] == '0')) { snprintf(err, errsz, "Invalid aggregate name"); goto out; }
    snprintf(in->group, sizeof in->group, "%s", s);
    s = ffn_json_get_string(doc, "token");
    if (!uuid_text(s) || boot_id(current_boot, sizeof current_boot)) { snprintf(err, errsz, "Fresh owner token and DP lifetime required"); goto out; }
    snprintf(in->token, sizeof in->token, "%s", s);
    s = ffn_json_get_string(doc, "boot_id");
    if (!s || strcmp(s, current_boot)) { snprintf(err, errsz, "Fresh owner token and DP lifetime required"); goto out; }
    snprintf(in->boot_id, sizeof in->boot_id, "%s", s);
    s = ffn_json_get_string(doc, "system");
    if (parse_mac(s, in->system) || (in->system[0] & 1)) { snprintf(err, errsz, "Unicast system MAC required"); goto out; }
    mac_text(in->system, in->system_text);
    members = ffn_json_get(doc, "members");
    if (members->type != FFN_JSON_ARRAY || members->count < 2 || members->count > MEMBERS) { snprintf(err, errsz, "Invalid optical members"); goto out; }
    in->count = (unsigned)members->count;
    for (unsigned i = 0; i < in->count; i++) {
        struct ffn_json *p = members->items[i];
        if (p->type != FFN_JSON_INT || front_source((unsigned)p->integer) < 0) { snprintf(err, errsz, "Invalid optical members"); goto out; }
        for (unsigned j = 0; j < i; j++) if (in->members[j] == p->integer) { snprintf(err, errsz, "Invalid optical members"); goto out; }
        in->members[i] = (uint16_t)p->integer;
    }
    memcpy(in->sorted, in->members, sizeof in->sorted);
    qsort(in->sorted, in->count, sizeof in->sorted[0], compare_ports);
    for (unsigned i = 0; i < 3; i++) {
        static const char *flags[] = { "control_only", "lldp", "offload" };
        if (!ffn_json_get_bool(doc, flags[i], &flag)) { snprintf(err, errsz, "Invalid owner flags"); goto out; }
    }
    ffn_json_get_bool(doc, "control_only", &in->control_only); ffn_json_get_bool(doc, "lldp", &in->lldp); ffn_json_get_bool(doc, "offload", &in->offload);
    if (in->control_only && in->offload) { snprintf(err, errsz, "Invalid owner flags"); goto out; }
    network = ffn_json_get(doc, "network");
    if (network->type != FFN_JSON_OBJECT || !ffn_json_get_int(network, "mtu", &value) || value < 576 || value > 1500 ||
        !ffn_json_get_bool(network, "dhcp", &flag) || !ffn_json_get_bool(network, "dhcp_default_route", &flag) ||
        !ffn_json_get_int(network, "dhcp_route_metric", &value) || !ffn_json_get(network, "addresses") ||
        ffn_json_get(network, "addresses")->type != FFN_JSON_ARRAY || !ffn_json_get(network, "management")) {
        snprintf(err, errsz, "Invalid aggregate network settings"); goto out;
    }
    lacp = ffn_json_get(doc, "lacp");
    if (lacp->type != FFN_JSON_OBJECT || lacp->count != 4) { snprintf(err, errsz, "Invalid LACP settings"); goto out; }
    s = ffn_json_get_string(lacp, "activity");
    if (!s || (strcmp(s, "active") && strcmp(s, "passive"))) { snprintf(err, errsz, "Invalid LACP mode"); goto out; }
    in->active = !strcmp(s, "active");
    s = ffn_json_get_string(lacp, "rate");
    if (!s || (strcmp(s, "fast") && strcmp(s, "slow"))) { snprintf(err, errsz, "Invalid LACP mode"); goto out; }
    in->fast = !strcmp(s, "fast");
    if (!ffn_json_get_int(lacp, "min_links", &value) || value < 1 || value > in->count) { snprintf(err, errsz, "Invalid minimum links"); goto out; }
    in->min_links = (unsigned)value;
    if (!ffn_json_get_int(lacp, "system_priority", &value) || value < 1 || value > 65535) { snprintf(err, errsz, "Invalid system priority"); goto out; }
    in->priority = (unsigned)value;
    in->doc = doc;
    d->network = ffn_json_copy(network);
    return 0;
invalid:
    snprintf(err, errsz, "Invalid aggregate owner intent");
out:
    ffn_json_free(doc);
    return -1;
}

/* dict(intent, network=..., lldp=..., network_generation=generation) */
static int write_intent_file(struct daemon *d, const struct ffn_json *network, bool lldp, const char *generation)
{
    struct ffn_json *doc = ffn_json_copy(d->in.doc);
    int rc;
    if (!doc) return -1;
    ffn_json_set(doc, "network", ffn_json_copy(network));
    ffn_json_set(doc, "lldp", ffn_json_bool(lldp));
    ffn_json_set(doc, "network_generation", ffn_json_string(generation));
    rc = write_json(d->intent_path, doc);
    ffn_json_free(doc);
    return rc;
}

static bool network_carrying(const struct ffn_json *network)
{
    bool enabled = true;
    struct ffn_json *units = ffn_json_get(network, "units");
    ffn_json_get_bool(network, "enabled", &enabled);
    return enabled || (units && units->type == FFN_JSON_ARRAY && units->count);
}

static bool network_dhcp(const struct ffn_json *network) { bool v = false; ffn_json_get_bool(network, "dhcp", &v); return v; }

/* --------------------------------------------------------------- the locks */

static int take_lock(struct daemon *d, const char *name, int operation, char *err, size_t errsz)
{
    char path[PATH];
    int fd;
    snprintf(path, sizeof path, "%s/%s", RUN, name);
    fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0 || flock(fd, operation | LOCK_NB)) {
        snprintf(err, errsz, "%s: %s", name, fd < 0 ? strerror(errno) : "lock busy");
        if (fd >= 0) close(fd);
        return -1;
    }
    d->locks[d->lock_count++] = fd;
    return 0;
}

static void release_locks(struct daemon *d)
{
    for (unsigned i = 0; i < d->lock_count; i++) close(d->locks[i]);
    d->lock_count = 0;
}

/* flock on a lock other processes hold for seconds at a time, with the loop
 * alive: the hosted paths are renewed while this waits, and the wait is
 * bounded, so a stuck holder costs this one step (EBUSY), not the ports already
 * serving. */
static int lock_wait(int fd, int operation, uint64_t budget)
{
    uint64_t start = now_us();
    for (;;) {
        if (!flock(fd, operation | LOCK_NB)) return 0;
        if (errno != EWOULDBLOCK && errno != EINTR) return -1;
        if (now_us() - start >= budget) { errno = EBUSY; return -1; }
        leases_renew();
        usleep(20000);
    }
}

/* ----------------------------------------------------------- environment */

static int check_environment(char *err, size_t errsz)
{
    char path[PATH], *text; size_t len;
    long value;
    if (unchecked) return 0;
    if (read_file("/proc/cpuinfo", &text, &len)) { snprintf(err, errsz, "OCTEON dataplane required"); return -1; }
    for (char *p = text; *p; p++) *p = (char)tolower((unsigned char)*p);
    if (!strstr(text, "octeon")) { free(text); snprintf(err, errsz, "OCTEON dataplane required"); return -1; }
    free(text);
    /* validate_trunk: physical, Ethernet, up, MTU >= 1550 */
    snprintf(path, sizeof path, "/sys/class/net/%s/device", TRUNK);
    if (access(path, F_OK)) { snprintf(err, errsz, "trunk must be a physical netdevice"); return -1; }
    snprintf(path, sizeof path, "/sys/class/net/%s/type", TRUNK);
    if (read_file(path, &text, &len)) { snprintf(err, errsz, "trunk unavailable"); return -1; }
    value = strtol(text, NULL, 10); free(text);
    if (value != 1) { snprintf(err, errsz, "trunk must be Ethernet, enabled, with MTU >= 1550"); return -1; }
    snprintf(path, sizeof path, "/sys/class/net/%s/mtu", TRUNK);
    if (read_file(path, &text, &len)) { snprintf(err, errsz, "trunk unavailable"); return -1; }
    value = strtol(text, NULL, 10); free(text);
    if (value < MAX_FRAME + 32) { snprintf(err, errsz, "trunk must be Ethernet, enabled, with MTU >= 1550"); return -1; }
    snprintf(path, sizeof path, "/sys/class/net/%s/flags", TRUNK);
    if (read_file(path, &text, &len)) { snprintf(err, errsz, "trunk unavailable"); return -1; }
    value = strtol(text, NULL, 16); free(text);
    if (!(value & 1)) { snprintf(err, errsz, "trunk must be Ethernet, enabled, with MTU >= 1550"); return -1; }
    return 0;
}

/* ------------------------------------------------------------ LACP driver */
/* The engine's gate driver records what it asked for; the native data path
 * is programmed from that record by datapath_sync, which also carries the
 * deadlines, the inspection mask and the readiness flag the engine knows
 * nothing about. The record reads back exactly, as the Python Gates did. */
static int gate_driver(void *ctx, const uint16_t *ports, struct ffn_lacp_gate *gates, unsigned count, char *err, size_t errsz)
{
    struct daemon *d = ctx;
    (void)ports; (void)err; (void)errsz;
    memcpy(d->gate, gates, count * sizeof *gates);
    d->gate_valid = true;
    return 0;
}

static int engine_create(struct daemon *d, char *err, size_t errsz)
{
    struct ffn_lacp_config c;
    memset(&c, 0, sizeof c);
    memcpy(c.system, d->in.system, 6);
    c.key = (uint16_t)d->in.tid; c.system_priority = (uint16_t)d->in.priority;
    c.active = d->in.active; c.fast = d->in.fast; c.min_links = d->in.min_links;
    c.collecting = !d->in.control_only; c.count = d->in.count;
    for (unsigned i = 0; i < d->in.count; i++) { c.members[i].port = d->in.members[i]; memcpy(c.members[i].source, d->in.system, 6); }
    d->engine = ffn_lacp_create(&c, gate_driver, d, err, errsz);
    return d->engine ? 0 : -1;
}

/* ------------------------------------------------------- control socket */

static int control_open(struct daemon *d, char *err, size_t errsz)
{
    uint32_t sources[2 * MEMBERS]; unsigned n = 0;
    for (unsigned i = 0; i < d->in.count; i++) sources[n++] = (uint32_t)front_source(d->in.members[i]);
    if (d->off.enabled) for (unsigned i = 0; i < d->in.count; i++) sources[n++] = d->off.alias[i];
    d->control_fd = ffn_aggregate_control_open(TRUNK, sources, n);
    if (d->control_fd < 0) { snprintf(err, errsz, "control socket: %s", strerror(errno)); return -1; }
    fcntl(d->control_fd, F_SETFL, O_NONBLOCK);
    if (ffn_aggregate_control_fe100(d->control_fd, d->native_members, d->in.count, NULL, 0, 0)) {
        snprintf(err, errsz, "control filter: %s", strerror(errno)); return -1;
    }
    return 0;
}

/* The trunk's injected header: 0x01, BE16 BCM port, 0x00, the MACs, eight
 * zero bytes (RAW_DSA), the ethertype onward; the Ethernet frame padded to 60
 * first so it cannot become a runt after the switch strips the header. */
static int trunk_send(struct daemon *d, unsigned port, const uint8_t *frame, size_t len)
{
    uint8_t packet[MAX_FRAME + 12];
    size_t body = len < 60 ? 60 : len, total = body + 12;
    int source = front_source(port);
    ssize_t sent;
    if (source < 0 || len < 14 || len > MAX_FRAME) { errno = EINVAL; return -1; }
    memset(packet, 0, total);
    packet[0] = 1; packet[1] = (uint8_t)(source >> 8); packet[2] = (uint8_t)source; packet[3] = 0;
    memcpy(packet + 4, frame, 12);
    memcpy(packet + 24, frame + 12, len - 12);
    sent = send(d->control_fd, packet, total, MSG_NOSIGNAL);
    if (sent < 0) return -1;
    if ((size_t)sent != total) { errno = EIO; return -1; }
    return 0;
}

static void lacp_service(struct daemon *d, uint64_t now)
{
    struct ffn_lacp_transmit out[MEMBERS];
    int n = ffn_lacp_transmissions(d->engine, now, out, MEMBERS);
    for (int i = 0; i < n; i++) {
        if (trunk_send(d, out[i].port, out[i].frame, FFN_LACP_PDU)) {
            char reason[128];
            snprintf(reason, sizeof reason, "LACP member transmit failed: %s", strerror(errno));
            ffn_lacp_abort(d->engine, reason, now_us());
            break;
        }
    }
}

/* A control frame from the trunk: the OTMH prefix names the source port; only
 * a member's slow-protocol frame reaches the engine. */
static void control_receive(struct daemon *d, uint64_t now)
{
    uint8_t raw[MAX_FRAME + 80];
    for (unsigned i = 0; i < 128; i++) {
        int n = ffn_aggregate_control_receive(d->control_fd, d->native_members, d->in.count, d->control_bindings,
                                              d->control_binding_count, d->control_deadline_ms, raw, sizeof raw);
        int port;
        if (n < 0) break;
        if (n < 18 || n > MAX_FRAME + 4 || raw[0] != 0 || raw[1] != 0x18) continue;
        port = front_port(be16(raw + 2));
        if (port < 0 || sorted_index(d, (unsigned)port) < 0) continue;
        if (be16(raw + 16) != 0x8809) continue;
        ffn_lacp_receive(d->engine, (uint16_t)port, raw + 4, (size_t)n - 4, now_us());
    }
    (void)now;
}

/* LLDP from the owner itself (legacy intent flag; ffn-lldpd is the agent). */
static void lldp_announce(struct daemon *d)
{
    for (unsigned i = 0; i < d->in.count; i++) {
        struct ffn_lacp_member_status m;
        uint8_t frame[256], *p = frame;
        char text[64];
        unsigned port = d->in.members[i];
        if (ffn_lacp_member(d->engine, (uint16_t)port, &m) || !m.link) continue;
        memcpy(p, (const uint8_t[]){ 0x01, 0x80, 0xc2, 0, 0, 0x0e }, 6); p += 6;
        memcpy(p, d->in.system, 6); p += 6;
        *p++ = 0x88; *p++ = 0xcc;
#define TLV(kind, body, n) do { *p++ = (uint8_t)(((kind) << 1) | (((n) >> 8) & 1)); *p++ = (uint8_t)(n); memcpy(p, body, n); p += n; } while (0)
        { uint8_t chassis[7] = { 4 }; memcpy(chassis + 1, d->in.system, 6); TLV(1, chassis, 7); }
        snprintf(text, sizeof text, "%cethernet1/%u", 5, port); TLV(2, text, strlen(text + 1) + 1);
        { uint8_t ttl[2] = { 0, 120 }; TLV(3, ttl, 2); }
        snprintf(text, sizeof text, "FFN aggregate %s", d->in.group); TLV(4, text, strlen(text));
        TLV(5, "FFN-NGFW", 8);
        *p++ = 0; *p++ = 0;
#undef TLV
        if (trunk_send(d, port, frame, (size_t)(p - frame))) fail(d, "Short LLDP write");
    }
}

/* ----------------------------------------------------------------- offload */

static void offload_init(struct daemon *d)
{
    struct offload *o = &d->off;
    memset(o, 0, sizeof *o);
    o->enabled = d->in.offload;
    for (unsigned i = 0; i < d->in.count; i++) o->alias[i] = o->enabled ? (0x8000u | (i << 8) | d->in.tid) : 0;
}

static void offload_acknowledge(struct daemon *d, const struct ffn_json *value, uint64_t now, double age, char *err, size_t errsz)
{
    struct offload *o = &d->off;
    struct ffn_json *ports;
    long long tid, psc;
    bool verified, exists;
    const char *metadata, *lb_key;
    o->member_count = 0; o->has_deadline = false;
    ffn_json_free(o->key_program); o->key_program = NULL;
    if (!value || value->type == FFN_JSON_NULL) return;
    ports = value->type == FFN_JSON_OBJECT ? ffn_json_get(value, "members") : NULL;
    metadata = value->type == FFN_JSON_OBJECT ? ffn_json_get_string(value, "ingress_metadata") : NULL;
    if (value->type != FFN_JSON_OBJECT || !ffn_json_get_int(value, "tid", &tid) || tid != (long long)d->in.tid ||
        !ffn_json_get_bool(value, "verified", &verified) || !verified || !ffn_json_get_bool(value, "exists", &exists) || !exists ||
        !ffn_json_get_int(value, "psc", &psc) || psc != 9 || !metadata || strcmp(metadata, "physical-or-fixed-spa") ||
        !ports || ports->type != FFN_JSON_ARRAY || !(age >= 0 && age < 5)) {
        snprintf(err, errsz, "Invalid hardware offload acknowledgement"); return;
    }
    if (ports->count && ports->count != d->in.count) { snprintf(err, errsz, "Invalid hardware offload acknowledgement"); return; }
    for (size_t i = 0; i < ports->count; i++) {
        if (ports->items[i]->type != FFN_JSON_INT || ports->items[i]->integer != d->in.sorted[i]) {
            snprintf(err, errsz, "Invalid hardware offload acknowledgement"); return;
        }
    }
    /* An unverified key program is a software-selection condition, not a fault. */
    lb_key = ffn_json_get_string(value, "lb_key");
    if (!lb_key || strcmp(lb_key, "pmf-dsa")) return;
    o->key_program = ffn_json_get(value, "key_program") ? ffn_json_copy(ffn_json_get(value, "key_program")) : NULL;
    for (size_t i = 0; i < ports->count; i++) o->members[i] = (unsigned)ports->items[i]->integer;
    o->member_count = (unsigned)ports->count;
    o->deadline = now + OBSERVATION_LEASE - (uint64_t)(age * SECOND);
    o->has_deadline = true;
}

/* The hardware destination is usable only while the acknowledged member set
 * is exactly the distributing set. */
static bool offload_ready(const struct daemon *d, uint64_t now)
{
    const struct offload *o = &d->off;
    unsigned n = 0;
    if (!o->enabled || !o->member_count || !o->has_deadline || now >= o->deadline) return false;
    for (unsigned i = 0; i < d->in.count; i++) {
        int e = -1;
        for (unsigned j = 0; j < d->in.count; j++) if (d->in.members[j] == d->in.sorted[i]) e = (int)j;
        if (e >= 0 && d->gate_valid && d->gate[e].distribute) {
            if (n >= o->member_count || o->members[n] != d->in.sorted[i]) return false;
            n++;
        }
    }
    return n == o->member_count;
}

/* -------------------------------------------------------------- inspection */

static const char *DETECTORS[3] = { "credit_card", "ssn", "api_key" };

static struct ffn_json *inspection_default(void)
{
    struct ffn_json *cfg = ffn_json_object();
    ffn_json_set(cfg, "revision", ffn_json_int(0)); ffn_json_set(cfg, "mode", ffn_json_string("off"));
    ffn_json_set(cfg, "ports", ffn_json_array()); ffn_json_set(cfg, "literal", ffn_json_string(""));
    return cfg;
}

static int inspection_validate(const struct ffn_json *cfg, char *err, size_t errsz)
{
    long long revision;
    const char *mode, *literal;
    struct ffn_json *ports, *detectors;
    if (!cfg || cfg->type != FFN_JSON_OBJECT || !(cfg->count == 4 || (cfg->count == 5 && ffn_json_get(cfg, "detectors"))) ||
        !ffn_json_get(cfg, "revision") || !ffn_json_get(cfg, "mode") || !ffn_json_get(cfg, "ports") || !ffn_json_get(cfg, "literal")) {
        snprintf(err, errsz, "expected revision, mode, ports, literal"); return -1;
    }
    if (!ffn_json_get_int(cfg, "revision", &revision) || revision < 0) { snprintf(err, errsz, "invalid revision"); return -1; }
    mode = ffn_json_get_string(cfg, "mode");
    if (!mode || (strcmp(mode, "off") && strcmp(mode, "alert") && strcmp(mode, "block"))) { snprintf(err, errsz, "mode must be off, alert or block"); return -1; }
    ports = ffn_json_get(cfg, "ports");
    if (ports->type != FFN_JSON_ARRAY) { snprintf(err, errsz, "inspection ports must be physical ports 1 through 24"); return -1; }
    for (size_t i = 0; i < ports->count; i++) {
        if (ports->items[i]->type != FFN_JSON_INT || ports->items[i]->integer < 1 || ports->items[i]->integer > 24) {
            snprintf(err, errsz, "inspection ports must be physical ports 1 through 24"); return -1;
        }
        for (size_t j = 0; j < i; j++) if (ports->items[j]->integer == ports->items[i]->integer) { snprintf(err, errsz, "duplicate port"); return -1; }
    }
    literal = ffn_json_get_string(cfg, "literal");
    if (!literal || strlen(literal) > 63) { snprintf(err, errsz, "literal must contain at most 63 printable ASCII characters"); return -1; }
    for (const char *p = literal; *p; p++) if (*p < 32 || *p > 126) { snprintf(err, errsz, "literal must contain at most 63 printable ASCII characters"); return -1; }
    detectors = ffn_json_get(cfg, "detectors");
    if (detectors) {
        if (detectors->type != FFN_JSON_ARRAY) { snprintf(err, errsz, "detectors must be a unique list of credit_card, ssn, api_key"); return -1; }
        for (size_t i = 0; i < detectors->count; i++) {
            const char *name = detectors->items[i]->type == FFN_JSON_STRING ? detectors->items[i]->string : NULL;
            bool known = false;
            for (unsigned k = 0; name && k < 3; k++) if (!strcmp(name, DETECTORS[k])) known = true;
            for (size_t j = 0; j < i; j++) if (ffn_json_equal(detectors->items[j], detectors->items[i])) known = false;
            if (!known) { snprintf(err, errsz, "detectors must be a unique list of credit_card, ssn, api_key"); return -1; }
        }
    }
    if (strcmp(mode, "off") && ((!literal[0] && (!detectors || !detectors->count)) || !ports->count)) {
        snprintf(err, errsz, "enabled policy requires a literal and ports"); return -1;
    }
    return 0;
}

static void *inspection_engine(struct inspector *in, const struct ffn_json *cfg, char *err, size_t errsz)
{
    const char *literal = ffn_json_get_string(cfg, "literal"), *mode = ffn_json_get_string(cfg, "mode");
    struct ffn_json *detectors = ffn_json_get(cfg, "detectors");
    int action = !strcmp(mode, "alert") ? 1 : 2;
    void *handle;
    if (!in->lib) {
        in->lib = dlopen(INLINE_LIBRARY, RTLD_NOW);
        if (!in->lib) { snprintf(err, errsz, "%s", dlerror()); return NULL; }
        *(void **)&in->create = dlsym(in->lib, "ffn_inline_create");
        *(void **)&in->create_profile = dlsym(in->lib, "ffn_inline_create_profile");
        *(void **)&in->destroy = dlsym(in->lib, "ffn_inline_destroy");
        in->scan = dlsym(in->lib, "ffn_inline_scan");
        if (!in->create || !in->destroy || !in->scan) { snprintf(err, errsz, "inline engine library lacks its API"); return NULL; }
    }
    if (detectors && detectors->count) {
        unsigned mask = 0;
        if (!in->create_profile) { snprintf(err, errsz, "Dataplane library does not support these detectors"); return NULL; }
        for (size_t i = 0; i < detectors->count; i++)
            for (unsigned k = 0; k < 3; k++) if (!strcmp(detectors->items[i]->string, DETECTORS[k])) mask |= 1u << k;
        handle = in->create_profile(literal, (unsigned)strlen(literal), action, mask);
    } else handle = in->create(literal, (unsigned)strlen(literal), action);
    if (!handle) snprintf(err, errsz, "engine allocation failed");
    return handle;
}

static void inspection_publish(struct inspector *in)
{
    struct ffn_json *doc = ffn_json_object();
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ffn_json_set(doc, "pid", ffn_json_int(getpid()));
    ffn_json_set(doc, "updated_at", ffn_json_number((double)ts.tv_sec + (double)ts.tv_nsec / 1e9));
    ffn_json_set(doc, "revision", ffn_json_copy(ffn_json_get(in->cfg, "revision")));
    ffn_json_set(doc, "mode", ffn_json_copy(ffn_json_get(in->cfg, "mode")));
    ffn_json_set(doc, "ports", ffn_json_copy(ffn_json_get(in->cfg, "ports")));
    ffn_json_set(doc, "counters", ffn_json_copy(in->counts));
    ffn_json_set(doc, "detectors", ffn_json_get(in->cfg, "detectors") ? ffn_json_copy(ffn_json_get(in->cfg, "detectors")) : ffn_json_array());
    ffn_json_set(doc, "execution", ffn_json_string("dataplane-cpu"));
    ffn_json_set(doc, "reload_error", in->has_error ? ffn_json_string(in->error) : ffn_json_null());
    write_json(in->status_path, doc);
    ffn_json_free(doc);
}

/* Once a second: reload a changed policy, retaining the last good one on failure. */
static void inspection_tick(struct inspector *in, uint64_t now)
{
    struct ffn_json *cfg = NULL;
    char err[128];
    if (now < in->next_poll) return;
    in->next_poll = now + SECOND;
    cfg = read_json(INSPECTION_POLICY);
    if (!cfg && errno == ENOENT) cfg = inspection_default();
    else if (!cfg || inspection_validate(cfg, err, sizeof err)) {
        if (!cfg) snprintf(err, sizeof err, "inspection policy unreadable");
        ffn_json_free(cfg); cfg = NULL;
        in->has_error = true; snprintf(in->error, sizeof in->error, "%s", err);
        inspection_publish(in);
        return;
    }
    if (!ffn_json_equal(cfg, in->cfg)) {
        void *handle = NULL;
        if (strcmp(ffn_json_get_string(cfg, "mode"), "off")) {
            handle = inspection_engine(in, cfg, err, sizeof err);
            if (!handle) {
                in->has_error = true; snprintf(in->error, sizeof in->error, "%s", err);
                ffn_json_free(cfg); inspection_publish(in); return;
            }
        }
        if (in->handle) in->destroy(in->handle);
        in->handle = handle;
        ffn_json_free(in->cfg); in->cfg = cfg;
    } else ffn_json_free(cfg);
    in->has_error = false;
    inspection_publish(in);
}

static bool inspection_port(const struct inspector *in, unsigned port)
{
    struct ffn_json *ports = in->cfg ? ffn_json_get(in->cfg, "ports") : NULL;
    if (!in->handle || !ports) return false;
    for (size_t i = 0; i < ports->count; i++) if (ports->items[i]->integer == (long long)port) return true;
    return false;
}

/* status_path NULL publishes to the shared /run/ffn-inspection.json (the WAN
 * owner's place); every other owner keeps its own file. */
static int inspection_open(struct daemon *d, const char *status_path)
{
    struct inspector *in = &d->insp;
    memset(in, 0, sizeof *in);
    if (status_path) snprintf(in->status_path, sizeof in->status_path, "%s", status_path);
    else snprintf(in->status_path, sizeof in->status_path, "%s/ffn-inspection.json", RUN);
    in->cfg = inspection_default();
    in->counts = ffn_json_object();
    inspection_tick(in, now_us());
    if (in->has_error) { fail(d, "Initial inspection configuration failed: %s", in->error); return -1; }
    return 0;
}

static void inspection_close(struct inspector *in)
{
    if (in->handle) in->destroy(in->handle);
    in->handle = NULL;
    if (in->status_path[0]) unlink(in->status_path);
    ffn_json_free(in->cfg); in->cfg = NULL;
    ffn_json_free(in->counts); in->counts = NULL;
}

/* --------------------------------------------------------- native owner */

static unsigned lanes_for(const char *device)
{
    struct ffn_json *policy = read_json(LANES_POLICY), *value;
    long long n = 1;
    if (!policy || policy->type != FFN_JSON_OBJECT) { ffn_json_free(policy); return 1; }
    value = ffn_json_get(policy, device);
    if (!value) value = ffn_json_get(policy, "default");
    if (value && value->type == FFN_JSON_INT && value->integer >= 1 && value->integer <= FFN_PACKET_LANES_MAX) n = value->integer;
    ffn_json_free(policy);
    return (unsigned)n;
}

/* The shared worker-CPU allocator stays one implementation (ffn_packet_cpus.py);
 * it records this process as the owner so the reservation lives as long as it. */
static int cpus_reserve(struct daemon *d, unsigned count, int *cpus, char *err, size_t errsz)
{
    struct helper h;
    char n[16], pid[32], line[256], perr[64];
    char *const argv[] = { (char *)PYTHON, (char *)CPUS, "reserve", "--count", n, "--pid", pid, NULL };
    struct ffn_json *reply = NULL, *list;
    const char *token;
    int status;
    snprintf(n, sizeof n, "%u", count); snprintf(pid, sizeof pid, "%d", (int)getpid());
    status = helper_run(&h, argv, NULL, 20 * SECOND);
    if (status == 0 && h.output.len) reply = ffn_json_parse(h.output.s, h.output.len, perr, sizeof perr);
    token = reply ? ffn_json_get_string(reply, "token") : NULL;
    list = reply ? ffn_json_get(reply, "cpus") : NULL;
    if (!token || strlen(token) != 32 || !list || list->type != FFN_JSON_ARRAY || list->count != count) {
        last_line(&h.error, line, sizeof line);
        snprintf(err, errsz, "CPU reservation: %s", line[0] ? line : status < 0 ? "timed out" : "unexpected reply");
        ffn_json_free(reply); helper_close(&h); return -1;
    }
    for (unsigned i = 0; i < count; i++) cpus[i] = (int)list->items[i]->integer;
    snprintf(d->cpu_token, sizeof d->cpu_token, "%s", token);
    ffn_json_free(reply); helper_close(&h);
    return 0;
}

static void cpus_release(struct daemon *d)
{
    char path[PATH];
    int fd;
    if (!d->cpu_token[0]) return;
    snprintf(path, sizeof path, "%s/lock", CPU_RESERVATIONS);
    fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (fd >= 0 && lock_wait(fd, LOCK_EX, LOCK_WAIT)) logmsg("cpu reservations lock: %s; releasing %s without it", strerror(errno), d->cpu_token);
    snprintf(path, sizeof path, "%s/%s.json", CPU_RESERVATIONS, d->cpu_token);
    unlink(path);
    if (fd >= 0) close(fd);
    d->cpu_token[0] = 0;
}

static void tap_steer(const char *device)
{
    struct helper h;
    char path[PATH], line[200];
    char *const argv[] = { (char *)PYTHON, (char *)STEERING, (char *)device, "--namespace", (char *)NS, NULL };
    snprintf(path, sizeof path, "/run/netns/%s", NS);
    if (access(path, F_OK)) return;
    if (helper_run(&h, argv, NULL, 15 * SECOND) != 0) {
        last_line(&h.error, line, sizeof line);
        logmsg("receive steering for %s not applied: %s", device, line);
    }
    helper_close(&h);
}

/* Lanes, worker CPUs and steering for an opened owner of `device`. */
static int native_start(struct daemon *d, const char *device, char *err, size_t errsz)
{
    int cpus[2 * FFN_PACKET_LANES_MAX], rx[FFN_PACKET_LANES_MAX], tx[FFN_PACKET_LANES_MAX];
    int lanes;
    d->lanes_wanted = lanes_for(device);
    lanes = ffn_packet_lanes(d->native, d->lanes_wanted);
    if (lanes < 1) { snprintf(err, errsz, "native lanes: %s", strerror(errno)); return -1; }
    d->lanes = (unsigned)lanes;
    if (cpus_reserve(d, 2 * d->lanes, cpus, err, errsz)) return -1;
    for (unsigned i = 0; i < d->lanes; i++) { rx[i] = cpus[2 * i]; tx[i] = cpus[2 * i + 1]; }
    if (ffn_packet_start_lanes(d->native, rx, tx, d->lanes)) { snprintf(err, errsz, "native workers: %s", strerror(errno)); return -1; }
    tap_steer(device);
    return 0;
}

static int native_open(struct daemon *d, char *err, size_t errsz)
{
    d->native = ffn_aggregate_open(TRUNK, NS, d->in.group, d->native_members, d->in.count, d->in.tid);
    if (!d->native) { snprintf(err, errsz, "native aggregate owner: %s", strerror(errno)); return -1; }
    return native_start(d, d->in.group, err, errsz);
}

static void native_close(struct daemon *d)
{
    /* Closing the packet sockets waits for an RCU grace period, long on a
     * loaded 40-core box; the other paths stay leased across it. */
    leases_renew();
    if (d->native) { ffn_packet_close(d->native); d->native = NULL; }
    leases_renew();
    cpus_release(d);
}

/* The parent's local addresses: static ones, or the DHCP lease's. */
static void local_addresses(struct daemon *d)
{
    d->local_count = 0;
    if (network_dhcp(d->network)) {
        const char *address = d->lease ? ffn_json_get_string(d->lease, "address") : NULL;
        const char *token = d->lease ? ffn_json_get_string(d->lease, "token") : NULL;
        if (address && token && !strcmp(token, d->in.token)) {
            char host[64]; snprintf(host, sizeof host, "%.*s", (int)strcspn(address, "/"), address);
            if (inet_pton(AF_INET, host, d->local[0]) == 1) { d->local_len[0] = 4; d->local_count = 1; }
        }
        return;
    }
    struct ffn_json *addresses = ffn_json_get(d->network, "addresses");
    for (size_t i = 0; addresses && i < addresses->count && d->local_count < 32; i++) {
        const char *a = addresses->items[i]->type == FFN_JSON_STRING ? addresses->items[i]->string : NULL;
        char host[64];
        if (!a) continue;
        snprintf(host, sizeof host, "%.*s", (int)strcspn(a, "/"), a);
        if (inet_pton(AF_INET, host, d->local[d->local_count]) == 1) d->local_len[d->local_count++] = 4;
        else if (inet_pton(AF_INET6, host, d->local[d->local_count]) == 1) d->local_len[d->local_count++] = 16;
    }
}

static void unit_row(struct ffn_aggregate_unit *row, unsigned tag, unsigned mtu, const struct ffn_json *addresses,
                     const uint8_t (*packed)[16], const unsigned *packed_len, unsigned packed_count)
{
    memset(row, 0, sizeof *row);
    row->tag = tag; row->mtu = mtu;
    for (unsigned i = 0; i < packed_count; i++) {
        if (packed_len[i] == 4 && row->n4 < 32) memcpy(row->local4[row->n4++], packed[i], 4);
        else if (packed_len[i] == 16 && row->n6 < 32) memcpy(row->local6[row->n6++], packed[i], 16);
    }
    for (size_t i = 0; addresses && i < addresses->count; i++) {
        const char *a = addresses->items[i]->type == FFN_JSON_STRING ? addresses->items[i]->string : NULL;
        char host[64]; uint8_t bin[16];
        if (!a) continue;
        snprintf(host, sizeof host, "%.*s", (int)strcspn(a, "/"), a);
        if (inet_pton(AF_INET, host, bin) == 1 && row->n4 < 32) memcpy(row->local4[row->n4++], bin, 4);
        else if (inet_pton(AF_INET6, host, bin) == 1 && row->n6 < 32) memcpy(row->local6[row->n6++], bin, 16);
    }
}

/* ffn_native_aggregate.units(): the parent row when enabled, then every unit. */
static unsigned build_rows(struct daemon *d, struct ffn_aggregate_unit *rows)
{
    unsigned n = 0;
    bool enabled = true;
    long long mtu = 1500;
    struct ffn_json *units = ffn_json_get(d->network, "units");
    ffn_json_get_bool(d->network, "enabled", &enabled);
    ffn_json_get_int(d->network, "mtu", &mtu);
    if (enabled) { unit_row(&rows[n++], 4096, (unsigned)mtu, NULL, d->local, d->local_len, d->local_count); snprintf(d->unit_name[4096], 24, "%s", d->in.group); }
    for (size_t i = 0; units && units->type == FFN_JSON_ARRAY && i < units->count && n < UNITS; i++) {
        struct ffn_json *u = units->items[i];
        long long tag, umtu;
        const char *name = ffn_json_get_string(u, "name");
        if (u->type != FFN_JSON_OBJECT || !ffn_json_get_int(u, "tag", &tag) || !ffn_json_get_int(u, "mtu", &umtu) || tag < 1 || tag > 4094) continue;
        unit_row(&rows[n++], (unsigned)tag, (unsigned)umtu, ffn_json_get(u, "addresses"), NULL, NULL, 0);
        snprintf(d->unit_name[tag], 24, "%s", name ? name : "");
    }
    return n;
}

/* fe100_return.py: this owner's bindings from the commissioning file. */
static struct ffn_json *fe100_config(void)
{
    struct ffn_json *raw = read_json(FE100_RETURN), *ports;
    long long trunk, return_port, lease = 3000, schema;
    if (!raw) return NULL;
    if (raw->type != FFN_JSON_OBJECT || !ffn_json_get_int(raw, "schema", &schema) || schema != 1 ||
        !ffn_json_get_int(raw, "trunk", &trunk) || trunk < 0 || trunk > 65535 ||
        !ffn_json_get_int(raw, "return_port", &return_port) || return_port < 0 || return_port > 65535 || trunk == return_port) goto bad;
    if (ffn_json_get(raw, "lease_ms") && (!ffn_json_get_int(raw, "lease_ms", &lease) || lease < 100 || lease > 10000)) goto bad;
    ports = ffn_json_get(raw, "ports");
    if (!ports || ports->type != FFN_JSON_OBJECT || ports->count > 8) goto bad;
    for (size_t i = 0; i < ports->count; i++) {
        struct ffn_json *entry = ports->items[i];
        long long in_lif, zone, number = atoll(ports->keys[i]);
        if (number < 1 || number > 63 || strspn(ports->keys[i], "0123456789") != strlen(ports->keys[i])) goto bad;
        if (entry->type != FFN_JSON_OBJECT || entry->count != 2 || !ffn_json_get_int(entry, "in_lif", &in_lif) || !ffn_json_get_int(entry, "zone", &zone) ||
            in_lif < 0 || in_lif > 65535 || zone < 0 || zone > 65535) goto bad;
    }
    return raw;
bad:
    ffn_json_free(raw);
    return NULL;
}

static unsigned fe100_bindings(const struct daemon *d, const struct ffn_json *config, struct ffn_packet_fe100_binding *rows, struct ffn_json **json)
{
    unsigned n = 0;
    struct ffn_json *ports = config ? ffn_json_get(config, "ports") : NULL;
    long long trunk = 0, return_port = 0;
    *json = ffn_json_array();
    if (!config) return 0;
    ffn_json_get_int(config, "trunk", &trunk); ffn_json_get_int(config, "return_port", &return_port);
    for (unsigned i = 0; i < d->front_count; i++) {
        char key[8]; struct ffn_json *entry, *row;
        long long in_lif = 0, zone = 0;
        unsigned front = d->fronts[i];
        snprintf(key, sizeof key, "%u", front);
        entry = ffn_json_get(ports, key);
        if (!entry) continue;
        ffn_json_get_int(entry, "in_lif", &in_lif); ffn_json_get_int(entry, "zone", &zone);
        rows[n].scope.trunk = (uint16_t)trunk; rows[n].scope.return_port = (uint16_t)return_port;
        rows[n].scope.front = (uint16_t)front; rows[n].scope.in_lif = (uint16_t)in_lif; rows[n].scope.zone = (uint16_t)zone;
        rows[n].source = (uint16_t)front_source(front);
        row = ffn_json_object();
        ffn_json_set(row, "trunk", ffn_json_int(trunk)); ffn_json_set(row, "return_port", ffn_json_int(return_port));
        ffn_json_set(row, "front", ffn_json_int(front)); ffn_json_set(row, "in_lif", ffn_json_int(in_lif));
        ffn_json_set(row, "zone", ffn_json_int(zone)); ffn_json_set(row, "source", ffn_json_int(rows[n].source));
        ffn_json_push(*json, row);
        n++;
    }
    return n;
}

/* Renew (or revoke once) the return bindings on the paused owner and the control socket. */
static void fe100_renew(struct daemon *d, uint64_t now)
{
    struct ffn_json *config = fe100_config(), *json;
    struct ffn_packet_fe100_binding rows[MEMBERS];
    unsigned n = fe100_bindings(d, config, rows, &json);
    long long lease = 3000;
    uint64_t deadline;
    if (config) ffn_json_get_int(config, "lease_ms", &lease);
    /* From the clock now, not the loop's `now`: the library refuses a deadline
     * already past, and a loop iteration that waited for a lock or a helper
     * started more than a lease ago ("FE100 return attachment: Invalid
     * argument" on every attach, 2026-10-21). */
    (void)now;
    deadline = now_us() / 1000 + (uint64_t)lease;
    if (n) {
        if (ffn_packet_fe100(d->native, rows, n, deadline)) fail(d, "FE100 return attachment: %s", strerror(errno));
        ffn_json_free(d->returns.rows); d->returns.rows = json; d->returns.deadline_ms = deadline; d->returns.active = true;
    } else {
        if (d->returns.active && ffn_packet_fe100(d->native, NULL, 0, 0)) fail(d, "FE100 return revocation: %s", strerror(errno));
        ffn_json_free(d->returns.rows); d->returns.rows = NULL; d->returns.active = false; ffn_json_free(json);
    }
    if (d->control_fd < 0) { /* a physical owner has no control socket */ }
    else if (n) {
        if (ffn_aggregate_control_fe100(d->control_fd, d->native_members, d->in.count, rows, n, deadline)) fail(d, "FE100 control attachment: %s", strerror(errno));
        memcpy(d->control_bindings, rows, n * sizeof *rows); d->control_binding_count = n; d->control_deadline_ms = deadline;
        d->control_returns.active = true;
    } else if (d->control_returns.active) {
        ffn_aggregate_control_fe100(d->control_fd, d->native_members, d->in.count, NULL, 0, 0);
        d->control_binding_count = 0; d->control_deadline_ms = 0; d->control_returns.active = false;
    }
    ffn_json_free(config);
}

/* Push the engine's gates, the network rows and the return bindings into the
 * native owner. Pauses the workers only when something changed. */
static void datapath_sync(struct daemon *d, uint64_t now, bool network_current)
{
    struct ffn_aggregate_unit rows[UNITS];
    unsigned n, collect = 0, distribute = 0, inspect = 0;
    uint64_t deadline = 0, offload_deadline = 0;
    bool rows_changed, gates_changed, renew = now >= d->next_fe100, paused = false;
    unsigned ready = network_current && network_carrying(d->network);
    void *engine = NULL;
    if (!d->native || d->failed) return;
    n = build_rows(d, rows);
    rows_changed = !d->rows_applied || n != d->row_count || memcmp(rows, d->rows, n * sizeof rows[0]) != 0;
    for (unsigned i = 0; i < d->in.count; i++) {
        unsigned port = d->in.sorted[i];
        struct ffn_lacp_member_status m;
        int e = -1;
        for (unsigned j = 0; j < d->in.count; j++) if (d->in.members[j] == port) e = (int)j;
        if (e < 0 || ffn_lacp_member(d->engine, (uint16_t)port, &m)) continue;
        if (d->gate_valid && d->gate[e].collect) collect |= 1u << i;
        if (d->gate_valid && d->gate[e].distribute) distribute |= 1u << i;
        if (d->gate_valid && (d->gate[e].collect || d->gate[e].distribute)) {
            uint64_t member_deadline = m.lease_us < m.deadline_us ? m.lease_us : m.deadline_us;
            if (!deadline || member_deadline < deadline) deadline = member_deadline;
        }
        if (inspection_port(&d->insp, port)) inspect |= 1u << i;
    }
    deadline /= 1000;
    if (offload_ready(d, now)) offload_deadline = d->off.deadline / 1000;
    if (inspect) engine = d->insp.handle;
    gates_changed = !d->gates_pushed || collect != d->gates_last.collect || distribute != d->gates_last.distribute ||
                    inspect != d->gates_last.inspect || deadline != d->gates_last.deadline ||
                    offload_deadline != d->gates_last.offload_deadline || ready != d->gates_last.ready || engine != d->gates_last.engine;
    if (!rows_changed && !gates_changed && !renew) return;
    if (ffn_packet_pause(d->native)) { fail(d, "native pause: %s", strerror(errno)); return; }
    paused = true;
    if (rows_changed) {
        if (ffn_aggregate_network(d->native, rows, n)) { fail(d, "native network: %s", strerror(errno)); goto resume; }
        memcpy(d->rows, rows, n * sizeof rows[0]); d->row_count = n; d->rows_applied = true;
    }
    if (gates_changed) {
        if (ffn_aggregate_gates(d->native, collect, distribute, inspect, deadline, offload_deadline, ready, engine, engine ? (ffn_scan_fn)d->insp.scan : NULL)) {
            fail(d, "native gates: %s", strerror(errno)); goto resume;
        }
        d->gates_last.collect = collect; d->gates_last.distribute = distribute; d->gates_last.inspect = inspect;
        d->gates_last.deadline = deadline; d->gates_last.offload_deadline = offload_deadline; d->gates_last.ready = ready;
        d->gates_last.engine = engine; d->gates_pushed = true;
    }
    if (renew) { fe100_renew(d, now); d->next_fe100 = now + SECOND; }
resume:
    if (paused && ffn_packet_resume(d->native)) fail(d, "native resume: %s", strerror(errno));
}

/* ffn_native_aggregate.AggregateOwner.snapshot / PacketOwner.workers */
static struct ffn_json *workers_json(struct daemon *d);

static void native_snapshot(struct daemon *d)
{
    uint64_t stats[8];
    if (!d->native) return;
    if (ffn_packet_counters(d->native, d->counters, FFN_PACKET_COUNTERS)) { fail(d, "native counters: %s", strerror(errno)); return; }
    for (unsigned i = 0; i < 6; i++) ffn_json_set(d->insp.counts, VERDICTS[i], ffn_json_int((long long)d->counters[8 + i]));
    if (ffn_aggregate_stats(d->native, d->agg_stats, 8)) { fail(d, "native aggregate stats: %s", strerror(errno)); return; }
    d->off.transmitted = d->agg_stats[3];
    for (unsigned i = 0; i < d->in.count; i++) {
        char key[40];
        if (ffn_aggregate_member_stats(d->native, i, stats, 8)) { fail(d, "native member stats: %s", strerror(errno)); return; }
        d->member_rx[i] = stats[0]; d->member_tx[i] = stats[1];
        for (unsigned v = 0; v < 6; v++) {
            snprintf(key, sizeof key, "port_%u_%s", d->in.sorted[i], VERDICTS[v]);
            ffn_json_set(d->insp.counts, key, ffn_json_int((long long)stats[2 + v]));
        }
    }
    ffn_json_free(d->unit_counts); d->unit_counts = ffn_json_object();
    for (unsigned r = 0; r < d->row_count; r++) {
        uint64_t values[4]; struct ffn_json *row;
        unsigned tag = d->rows[r].tag;
        if (ffn_aggregate_unit_stats(d->native, tag, values, 4)) continue;
        row = ffn_json_object();
        ffn_json_set(row, "rx_packets", ffn_json_int((long long)values[0])); ffn_json_set(row, "rx_bytes", ffn_json_int((long long)values[1]));
        ffn_json_set(row, "tx_packets", ffn_json_int((long long)values[2])); ffn_json_set(row, "tx_bytes", ffn_json_int((long long)values[3]));
        ffn_json_set(d->unit_counts, d->unit_name[tag], row);
    }
    ffn_json_free(d->workers); d->workers = workers_json(d);
}

/* PacketOwner.workers(): the worker threads, their CPUs and their batch statistics. */
static struct ffn_json *workers_json(struct daemon *d)
{
    int out[8];
    struct ffn_json *w = ffn_json_object();
    if (ffn_packet_workers(d->native, out, 8) == 0) {
        uint64_t batches[3] = { 0 }, sent[3] = { 0 }, punts[3] = { 0 };
        struct ffn_json *lanes = ffn_json_array(), *tx;
        unsigned count = ffn_packet_lane_count(d->native);
        ffn_packet_receive_stats(d->native, batches, 3);
        ffn_packet_transmit_stats(d->native, sent, 3);
        ffn_packet_fe100_stats(d->native, punts, 3);
        for (unsigned lane = 0; lane < count; lane++) {
            int row[5]; struct ffn_json *l;
            if (ffn_packet_lane_workers(d->native, lane, row, 5)) continue;
            l = ffn_json_object();
            ffn_json_set(l, "workers", ffn_json_int(row[0])); ffn_json_set(l, "rx_cpu", ffn_json_int(row[1]));
            ffn_json_set(l, "tx_cpu", ffn_json_int(row[2])); ffn_json_set(l, "rx_tid", ffn_json_int(row[3])); ffn_json_set(l, "tx_tid", ffn_json_int(row[4]));
            ffn_json_push(lanes, l);
        }
        ffn_json_set(w, "count", ffn_json_int(out[0])); ffn_json_set(w, "rx_cpu", ffn_json_int(out[1])); ffn_json_set(w, "tx_cpu", ffn_json_int(out[2]));
        ffn_json_set(w, "rx_tid", ffn_json_int(out[3])); ffn_json_set(w, "tx_tid", ffn_json_int(out[4]));
        ffn_json_set(w, "paused", ffn_json_bool(out[5])); ffn_json_set(w, "stopped", ffn_json_bool(out[6])); ffn_json_set(w, "error", ffn_json_int(out[7]));
        ffn_json_set(w, "scheduling", ffn_json_string(count > 1 ? "ordered-rx-tx-per-lane" : "ordered-rx-tx"));
        ffn_json_set(w, "flow_parallelism", ffn_json_bool(count > 1)); ffn_json_set(w, "lanes", lanes);
        ffn_json_set(w, "cpu_allocation", ffn_json_string("shared-reservations"));
        ffn_json_set(w, "receive_syscalls", ffn_json_int((long long)batches[0])); ffn_json_set(w, "receive_frames", ffn_json_int((long long)batches[1]));
        ffn_json_set(w, "receive_max_batch", ffn_json_int((long long)batches[2]));
        ffn_json_set(w, "transmit_syscalls", ffn_json_int((long long)sent[0])); ffn_json_set(w, "transmit_frames", ffn_json_int((long long)sent[1]));
        ffn_json_set(w, "transmit_max_batch", ffn_json_int((long long)sent[2]));
        tx = ffn_json_object();
        ffn_json_set(tx, "decoded", ffn_json_int((long long)punts[0])); ffn_json_set(tx, "rejected", ffn_json_int((long long)punts[1]));
        ffn_json_set(tx, "lease_expired", ffn_json_int((long long)punts[2]));
        ffn_json_set(w, "fe100_return", tx);
        ffn_json_set(w, "execution", ffn_json_string("native-c"));
    }
    return w;
}

/* ------------------------------------------------------ network helpers */

static void network_teardown(struct daemon *d)
{
    struct ffn_json *request = ffn_json_object(), *reply;
    char err[256];
    ffn_json_set(request, "group", ffn_json_string(d->in.group));
    ffn_json_set(request, "token", ffn_json_string(d->in.token));
    ffn_json_set(request, "management", ffn_json_copy(ffn_json_get(d->network, "management")));
    reply = runtime_verb("network-teardown", request, 45 * SECOND, err, sizeof err);
    ffn_json_free(request);
    if (!reply) { logmsg("network teardown incomplete, left for the watchdog: %s", err); d->created = false; return; }
    ffn_json_free(reply);
    d->created = false;
    unlink(d->intent_path);
}

static void dhcp_signal(pid_t pid, int sig) { if (pid > 0) kill(-pid, sig); }

static void dhcp_retire(struct daemon *d, uint64_t now)
{
    if (d->dhcp <= 0) return;
    if (!d->dhcp_exited) dhcp_signal(d->dhcp, SIGTERM);
    if (d->retired_count < MEMBERS) { d->retired[d->retired_count].pid = d->dhcp; d->retired[d->retired_count].at = now; d->retired_count++; }
    d->dhcp = 0; d->dhcp_exited = false;
}

static void dhcp_start(struct daemon *d)
{
    char pidfile[PATH], generation[96];
    char *const argv[] = { "/usr/sbin/ip", "netns", "exec", (char *)NS, "udhcpc", "-f", "-i", d->in.group, "-s", (char *)DHCP_HOOK, "-p", pidfile, NULL };
    pid_t pid;
    snprintf(pidfile, sizeof pidfile, "%s", d->dhcp_pid_path);
    snprintf(generation, sizeof generation, "FFN_AGGREGATE_NETWORK_REVISION=%s", d->lease_generation);
    pid = fork();
    if (pid < 0) { snprintf(d->network_error, sizeof d->network_error, "Aggregate DHCP client could not start: %s", strerror(errno)); d->has_network_error = true; return; }
    if (pid == 0) {
        int null = open("/dev/null", O_RDONLY);
        if (null >= 0) dup2(null, 0);
        dup2(2, 1);
        setsid();
        putenv(generation);
        execvp("ip", argv);
        _exit(127);
    }
    d->dhcp = pid; d->dhcp_exited = false;
}

static void children_service(struct daemon *d, uint64_t now)
{
    int status;
    if (d->dhcp > 0 && !d->dhcp_exited && waitpid(d->dhcp, &status, WNOHANG) == d->dhcp) d->dhcp_exited = true;
    for (unsigned i = 0; i < d->retired_count;) {
        if (waitpid(d->retired[i].pid, &status, WNOHANG) == d->retired[i].pid) { d->retired[i] = d->retired[--d->retired_count]; continue; }
        if (now - d->retired[i].at > RETIRED_GRACE) dhcp_signal(d->retired[i].pid, SIGKILL);
        i++;
    }
}

/* The network-update worker: a separate process, so IP/nft/lease work never
 * blocks LACP. */
static void worker_start(struct daemon *d, struct ffn_json *worker_intent)
{
    struct ffn_json *request = ffn_json_object();
    char *input;
    char *const argv[] = { (char *)PYTHON, (char *)RUNTIME, "network-update", NULL };
    ffn_json_set(request, "intent", ffn_json_copy(worker_intent));
    ffn_json_set(request, "previous", ffn_json_copy(d->network));
    input = ffn_json_dump(request);
    ffn_json_free(request);
    if (!input || helper_start(&d->worker, argv, input, NULL)) {
        snprintf(d->network_error, sizeof d->network_error, "Could not start network update: %s", strerror(errno)); d->has_network_error = true;
        free(input); return;
    }
    text_put(&d->worker.input, "\n", 1);
    free(input);
    d->worker_running = true;
    ffn_json_free(d->worker_intent); d->worker_intent = ffn_json_copy(worker_intent);
}

static void worker_finish(struct daemon *d)
{
    struct ffn_json *ack = NULL;
    char perr[64], line[512], revision[65];
    int status = helper_status(&d->worker);
    bool closed = helper_service(&d->worker);
    if (status < 0 || !closed) {
        if (status >= 0 || now_us() - d->worker.started > WORKER_TIMEOUT) {
            helper_kill(&d->worker); helper_service(&d->worker);
            snprintf(d->network_error, sizeof d->network_error, "Network update timed out; LACP retained, data delivery blocked"); d->has_network_error = true;
            helper_close(&d->worker); d->worker_running = false;
        }
        return;
    }
    if (d->worker.output.len) ack = ffn_json_parse(d->worker.output.s, d->worker.output.len, perr, sizeof perr);
    {
        bool ok = false;
        const char *got = ack ? ffn_json_get_string(ack, "revision") : NULL;
        bool lldp = false;
        ffn_json_get_bool(d->worker_intent, "lldp", &lldp);
        network_revision(ffn_json_get(d->worker_intent, "network"), lldp, revision);
        if (ack) ffn_json_get_bool(ack, "ok", &ok);
        if (status || !ok || !got || strcmp(got, revision)) {
            last_line(&d->worker.error, line, sizeof line);
            snprintf(d->network_error, sizeof d->network_error, "%s", line[0] ? line : "Network update failed"); d->has_network_error = true;
        } else {
            ffn_json_free(d->network); d->network = ffn_json_copy(ffn_json_get(d->worker_intent, "network"));
            d->in.lldp = lldp;
            snprintf(d->applied_revision, sizeof d->applied_revision, "%s", got);
            d->has_network_error = false;
            ffn_json_free(d->unit_status);
            d->unit_status = ffn_json_get(ack, "subinterfaces") ? ffn_json_copy(ffn_json_get(ack, "subinterfaces")) : ffn_json_array();
        }
    }
    ffn_json_free(ack);
    helper_close(&d->worker); d->worker_running = false;
}

/* A configuration the MP asked for that this owner has not attempted yet. */
static void configuration_service(struct daemon *d, uint64_t now)
{
    const char *revision = d->requested ? ffn_json_get_string(d->requested, "revision") : NULL;
    struct ffn_json *network, *worker_intent;
    bool lldp = false, retry;
    if (!revision || d->worker_running || !strcmp(revision, d->attempted_revision)) return;
    retry = d->has_network_error;
    snprintf(d->attempted_revision, sizeof d->attempted_revision, "%s", revision);
    d->has_network_error = false;
    network = ffn_json_get(d->requested, "network");
    ffn_json_get_bool(d->requested, "lldp", &lldp);
    worker_intent = ffn_json_copy(d->in.doc);
    ffn_json_set(worker_intent, "network", ffn_json_copy(network));
    ffn_json_set(worker_intent, "lldp", ffn_json_bool(lldp));
    if (d->in.control_only || (ffn_json_equal(network, d->network) && !retry)) {
        ffn_json_free(d->network); d->network = ffn_json_copy(network);
        d->in.lldp = lldp;
        snprintf(d->applied_revision, sizeof d->applied_revision, "%s", revision);
        write_intent_file(d, d->network, d->in.lldp, d->lease_generation);
    } else {
        /* Reject old DHCP hooks before signaling the previous client. */
        write_intent_file(d, network, lldp, revision);
        snprintf(d->lease_generation, sizeof d->lease_generation, "%s", revision);
        dhcp_retire(d, now);
        worker_start(d, worker_intent);
    }
    ffn_json_free(worker_intent);
}

/* ------------------------------------------------------------- status row */

static struct ffn_json *info_json(const struct ffn_lacp_info *i, bool flags)
{
    static const char *names[8] = { "activity", "short_timeout", "aggregation", "synchronization", "collecting", "distributing", "defaulted", "expired" };
    struct ffn_json *o = ffn_json_object();
    char mac[18];
    mac_text(i->system, mac);
    ffn_json_set(o, "system_priority", ffn_json_int(i->system_priority)); ffn_json_set(o, "system", ffn_json_string(mac));
    ffn_json_set(o, "key", ffn_json_int(i->key)); ffn_json_set(o, "port_priority", ffn_json_int(i->port_priority));
    ffn_json_set(o, "port", ffn_json_int(i->port)); ffn_json_set(o, "state", ffn_json_int(i->state));
    if (flags) {
        struct ffn_json *f = ffn_json_object();
        for (unsigned b = 0; b < 8; b++) ffn_json_set(f, names[b], ffn_json_bool(i->state & (1u << b)));
        ffn_json_set(o, "flags", f);
    }
    return o;
}

static struct ffn_json *gate_json(const struct ffn_lacp_gate *g)
{
    struct ffn_json *o = ffn_json_object();
    ffn_json_set(o, "collect", ffn_json_bool(g->collect)); ffn_json_set(o, "distribute", ffn_json_bool(g->distribute));
    return o;
}

static struct ffn_json *engine_status(struct daemon *d, uint64_t now)
{
    struct ffn_json *row = ffn_json_object(), *distributing = ffn_json_array(), *members = ffn_json_object();
    uint16_t ports[MEMBERS];
    unsigned n;
    const char *fault;
    ffn_lacp_tick(d->engine, now);
    fault = ffn_lacp_fault(d->engine);
    ffn_json_set(row, "fault", fault ? ffn_json_string(fault) : ffn_json_null());
    ffn_json_set(row, "gates_verified", ffn_json_bool(ffn_lacp_gates_verified(d->engine)));
    n = ffn_lacp_distributing(d->engine, ports, MEMBERS);
    for (unsigned i = 0; i < n; i++) ffn_json_push(distributing, ffn_json_int(ports[i]));
    ffn_json_set(row, "distributing", distributing);
    for (unsigned i = 0; i < d->in.count; i++) {
        struct ffn_lacp_member_status m; struct ffn_lacp_info actor; struct ffn_json *o = ffn_json_object();
        char key[8];
        ffn_lacp_member(d->engine, d->in.members[i], &m); ffn_lacp_actor(d->engine, d->in.members[i], &actor);
        ffn_json_set(o, "rx", ffn_json_string(ffn_lacp_rx_name(m.rx)));
        ffn_json_set(o, "selected", ffn_json_bool(m.selected)); ffn_json_set(o, "matched", ffn_json_bool(m.matched));
        ffn_json_set(o, "actor", info_json(&actor, false));
        ffn_json_set(o, "peer", m.has_peer ? info_json(&m.peer, true) : ffn_json_null());
        ffn_json_set(o, "tx_attempts", ffn_json_int((long long)m.tx_attempts)); ffn_json_set(o, "received", ffn_json_int((long long)m.received));
        ffn_json_set(o, "invalid", ffn_json_int((long long)m.invalid));
        ffn_json_set(o, "gates", m.has_gate ? gate_json(&m.gate) : ffn_json_null());
        snprintf(key, sizeof key, "%u", d->in.members[i]);
        ffn_json_set(members, key, o);
    }
    ffn_json_set(row, "members", members);
    return row;
}

/* The status row: the file the MP's `status` reads, and the same line to `out`
 * (the lease connection) when one is given. */
static void status_row(struct daemon *d, uint64_t now, bool network_current, bool network_ready, struct text *out)
{
    struct ffn_json *row = engine_status(d, now), *counters = ffn_json_object(), *rx = ffn_json_object(), *tx = ffn_json_object();
    struct ffn_json *subinterfaces = ffn_json_array();
    uint16_t ports[MEMBERS];
    bool distributing = ffn_lacp_distributing(d->engine, ports, MEMBERS) > 0;
    char *text;
    ffn_json_set(row, "group", ffn_json_string(d->in.group)); ffn_json_set(row, "token", ffn_json_string(d->in.token));
    ffn_json_set(row, "boot_id", ffn_json_string(d->in.boot_id)); ffn_json_set(row, "pid", ffn_json_int(getpid()));
    ffn_json_set(row, "process_start", ffn_json_string(d->process_start));
    ffn_json_set(row, "updated_monotonic", ffn_json_number((double)now / 1e6));
    ffn_json_set(row, "control_age_seconds", ffn_json_number(now > d->last_input ? (double)(now - d->last_input) / 1e6 : 0.0));
    ffn_json_set(row, "owner", ffn_json_string("ffn-portd"));
    ffn_json_set(row, "packet_execution", ffn_json_string(d->native ? "native-c" : "control-only"));
    ffn_json_set(row, "workers", d->native && d->workers ? ffn_json_copy(d->workers) : ffn_json_null());
    ffn_json_set(row, "control_only", ffn_json_bool(d->in.control_only));
    ffn_json_set(row, "attachment_ready", ffn_json_bool(d->native && network_carrying(d->network) && network_current));
    ffn_json_set(row, "configuration_ready", ffn_json_bool(network_current && !d->in.control_only));
    ffn_json_set(row, "configuration_revision", ffn_json_string(d->applied_revision));
    ffn_json_set(row, "network_error", d->has_network_error ? ffn_json_string(d->network_error) : ffn_json_null());
    ffn_json_set(row, "network_update_pending", ffn_json_bool(!network_current));
    for (size_t i = 0; d->unit_status && i < d->unit_status->count; i++) {
        struct ffn_json *u = ffn_json_copy(d->unit_status->items[i]);
        const char *name = ffn_json_get_string(u, "name");
        struct ffn_json *counts = name && d->unit_counts ? ffn_json_get(d->unit_counts, name) : NULL;
        ffn_json_set(u, "applied", ffn_json_bool(network_current));
        ffn_json_set(u, "state", ffn_json_string(network_current && distributing ? "active" : network_current ? "configured" : "pending"));
        ffn_json_set(u, "counters", counts ? ffn_json_copy(counts) : ffn_json_object());
        ffn_json_push(subinterfaces, u);
    }
    ffn_json_set(row, "subinterfaces", subinterfaces);
    ffn_json_set(row, "network", ffn_json_copy(d->network));
    {
        struct ffn_json *p = ffn_json_array();
        for (unsigned i = 0; i < d->in.count; i++) ffn_json_push(p, ffn_json_int(d->in.members[i]));
        ffn_json_set(row, "ports", p);
    }
    ffn_json_set(row, "hardware_offload", ffn_json_bool(offload_ready(d, now)));
    ffn_json_set(row, "offload_requested", ffn_json_bool(d->in.offload));
    ffn_json_set(row, "offload_tx", ffn_json_int((long long)(d->off.enabled ? d->off.transmitted : 0)));
    ffn_json_set(row, "offload_scope", ffn_json_string("BCM egress member selection only"));
    ffn_json_set(row, "transit_policy", ffn_json_string("default-deny"));
    if (d->native) {
        for (unsigned i = 0; i < FFN_PACKET_COUNTERS; i++) ffn_json_set(counters, COUNTER_NAMES[i], ffn_json_int((long long)d->counters[i]));
        for (unsigned i = 0; i < 8; i++) ffn_json_set(counters, AGGREGATE_STAT_NAMES[i], ffn_json_int((long long)d->agg_stats[i]));
        for (unsigned i = 0; i < d->in.count; i++) {
            char key[8]; snprintf(key, sizeof key, "%u", d->in.sorted[i]);
            ffn_json_set(rx, key, ffn_json_int((long long)d->member_rx[i])); ffn_json_set(tx, key, ffn_json_int((long long)d->member_tx[i]));
        }
    } else {
        for (unsigned i = 0; i < d->in.count; i++) {
            char key[8]; snprintf(key, sizeof key, "%u", d->in.members[i]);
            ffn_json_set(rx, key, ffn_json_int(0)); ffn_json_set(tx, key, ffn_json_int(0));
        }
    }
    ffn_json_set(row, "counters", counters);
    if (d->returns.active && d->returns.rows) {
        struct ffn_json *r = ffn_json_object();
        ffn_json_set(r, "rows", ffn_json_copy(d->returns.rows)); ffn_json_set(r, "deadline_ms", ffn_json_int((long long)d->returns.deadline_ms));
        ffn_json_set(row, "fe100_return", r);
    } else ffn_json_set(row, "fe100_return", ffn_json_null());
    ffn_json_set(row, "data_rx", rx); ffn_json_set(row, "data_tx", tx);
    ffn_json_set(row, "gate_drops", ffn_json_int((long long)(d->native ? d->agg_stats[0] : 0)));
    ffn_json_set(row, "network_ready", ffn_json_bool(network_ready));
    ffn_json_set(row, "lease", d->lease ? ffn_json_copy(d->lease) : ffn_json_object());
    text = ffn_json_dump(row);
    if (text) {
        write_atomic(d->status_path, text);
        if (out) { text_str(out, text); text_put(out, "\n", 1); }
    }
    free(text);
    ffn_json_free(row);
}

/* The final row of a lease that ends on a fault, to `out`. */
static void error_row(struct daemon *d, struct text *out)
{
    struct ffn_json *row = ffn_json_object();
    char *text;
    ffn_json_set(row, "error", ffn_json_string(d->fatal));
    ffn_json_set(row, "token", ffn_json_string(d->in.token));
    ffn_json_set(row, "boot_id", ffn_json_string(d->in.boot_id));
    ffn_json_set(row, "group", ffn_json_string(d->in.group));
    text = ffn_json_dump(row);
    if (text && out) { text_str(out, text); text_put(out, "\n", 1); }
    free(text); ffn_json_free(row);
}

/* ------------------------------------------------------------- control */

static void control_line(struct daemon *d, const char *line, size_t len, uint64_t now)
{
    char perr[64], err[128];
    struct ffn_json *msg = ffn_json_parse(line, len, perr, sizeof perr), *links, *config;
    const char *token;
    long long sequence;
    double age;
    uint64_t stamp;
    if (!msg || msg->type != FFN_JSON_OBJECT) { fail(d, "Invalid control message"); ffn_json_free(msg); return; }
    token = ffn_json_get_string(msg, "token");
    if (!token || strcmp(token, d->in.token) || !ffn_json_get_int(msg, "sequence", &sequence) || sequence <= d->sequence) {
        fail(d, "Stale aggregate observation"); ffn_json_free(msg); return;
    }
    d->sequence = sequence;
    if (!ffn_json_get_number(msg, "age_seconds", &age) || !(age >= 0 && age < 5)) { fail(d, "Expired CP observation"); ffn_json_free(msg); return; }
    links = ffn_json_get(msg, "links");
    if (!links || links->type != FFN_JSON_ARRAY || links->count != d->in.count) { fail(d, "Incomplete member observation"); ffn_json_free(msg); return; }
    for (size_t i = 0; i < links->count; i++) {
        long long port; bool seen = false;
        if (links->items[i]->type != FFN_JSON_OBJECT || !ffn_json_get_int(links->items[i], "port", &port)) { fail(d, "Incomplete member observation"); ffn_json_free(msg); return; }
        for (unsigned j = 0; j < d->in.count; j++) if (d->in.members[j] == port) seen = true;
        for (size_t j = 0; j < i; j++) { long long other; if (ffn_json_get_int(links->items[j], "port", &other) && other == port) seen = false; }
        if (!seen) { fail(d, "Incomplete member observation"); ffn_json_free(msg); return; }
    }
    stamp = now_us();
    for (size_t i = 0; i < links->count; i++) {
        long long port = 0, speed = 0; bool up = false;
        ffn_json_get_int(links->items[i], "port", &port); ffn_json_get_bool(links->items[i], "up", &up);
        ffn_json_get_int(links->items[i], "speed_mbps", &speed);
        if (ffn_lacp_link(d->engine, (uint16_t)port, up, up ? (uint32_t)speed : 0, stamp, OBSERVATION_LEASE - (uint64_t)(age * SECOND))) {
            fail(d, "Invalid carrier/speed"); ffn_json_free(msg); return;
        }
    }
    if (d->off.enabled) {
        err[0] = 0;
        offload_acknowledge(d, ffn_json_get(msg, "offload"), stamp, age, err, sizeof err);
        if (err[0]) { fail(d, "%s", err); ffn_json_free(msg); return; }
    }
    d->last_input = stamp;
    config = ffn_json_get(msg, "config");
    if (config) {
        char revision[65];
        const char *given = config->type == FFN_JSON_OBJECT ? ffn_json_get_string(config, "revision") : NULL;
        struct ffn_json *network = config->type == FFN_JSON_OBJECT ? ffn_json_get(config, "network") : NULL;
        bool lldp;
        if (!given || !network || network->type != FFN_JSON_OBJECT || config->count != 3 || !ffn_json_get_bool(config, "lldp", &lldp)) {
            fail(d, "Invalid network update"); ffn_json_free(msg); return;
        }
        network_revision(network, lldp, revision);
        if (strcmp(given, revision)) { fail(d, "Network revision mismatch"); ffn_json_free(msg); return; }
        ffn_json_free(d->requested); d->requested = ffn_json_copy(config);
    }
    (void)now;
    ffn_json_free(msg);
}

/* Every complete line the lease connection has delivered so far. */
static void lease_lines(struct daemon *d, struct text *buffer, uint64_t now)
{
    if (buffer->len > 262144) { fail(d, "Control input exceeds limit"); return; }
    for (;;) {
        char *nl = buffer->s ? memchr(buffer->s, '\n', buffer->len) : NULL;
        size_t len;
        if (!nl) break;
        len = (size_t)(nl - buffer->s);
        if (len) control_line(d, buffer->s, len, now);
        memmove(buffer->s, nl + 1, buffer->len - len - 1);
        buffer->len -= len + 1; buffer->s[buffer->len] = 0;
        if (d->failed) break;
    }
}

/* -------------------------------------------------------------- shutdown */

/* Withdraw an aggregate owner: gates, workers, helpers, DHCP, network, run
 * files, locks. The daemon's own shutdown bounds the whole of it with one
 * alarm; a single lease ending must not arm one for the other owners. */
static void shutdown_owner(struct daemon *d)
{
    uint64_t now = now_us();
    if (d->engine) ffn_lacp_stop(d->engine, now);
    if (d->native) { datapath_sync(d, now, false); native_close(d); }
    if (d->worker_running) { helper_kill(&d->worker); helper_close(&d->worker); d->worker_running = false; }
    for (unsigned i = 0; i < d->retired_count; i++) { dhcp_signal(d->retired[i].pid, SIGKILL); waitpid(d->retired[i].pid, NULL, 0); }
    d->retired_count = 0;
    if (d->dhcp > 0) {
        int status;
        uint64_t deadline = now_us() + 3 * SECOND;
        if (!d->dhcp_exited) dhcp_signal(d->dhcp, SIGTERM);
        while (!d->dhcp_exited && now_us() < deadline) {
            if (waitpid(d->dhcp, &status, WNOHANG) == d->dhcp) d->dhcp_exited = true; else usleep(50000);
        }
        if (!d->dhcp_exited) { dhcp_signal(d->dhcp, SIGKILL); waitpid(d->dhcp, NULL, 0); }
        d->dhcp = 0;
    }
    if (d->insp.counts) inspection_close(&d->insp);
    if (d->control_fd >= 0) { close(d->control_fd); d->control_fd = -1; }
    if (d->created) network_teardown(d);
    else unlink(d->intent_path);
    unlink(d->lease_path); unlink(d->pending_path); unlink(d->dhcp_pid_path);
    unlink(d->status_path);
    release_locks(d);
}

/* ------------------------------------------------------ hosted aggregate */
/* An aggregate owner inside the daemon: what `aggregate serve` did for one
 * group in one process, as a state machine the daemon's loop drives. Its
 * lease is a control-socket connection (the relay `aggregate serve` bridges
 * the MP supervisor's ssh pipe to it): the intent is the first line, then the
 * observation lines, and the status rows go back on it. Starting is
 * asynchronous through the two long helpers (validate, network-setup), so a
 * group with many VLAN units does not stall every other owner's status for
 * its setup; the rest of the start (native owner, control socket) is fast. */

struct aggregate;

struct client {
    int fd;
    struct text in, out;
    bool eof;                   /* the peer closed its side */
    bool closing;               /* close once the output drained */
    struct aggregate *agg;      /* the lease this connection holds, if any */
};

enum aggregate_stage { AGG_FREE, AGG_VALIDATING, AGG_SETTING_UP, AGG_RUNNING, AGG_STOPPED };

struct aggregate {
    enum aggregate_stage stage;
    struct daemon d;
    struct helper start;        /* the validate or network-setup helper in flight */
    uint64_t start_deadline;
    struct client *client;
    uint64_t last_tick;         /* when the loop last served this group (stall accounting) */
    bool current, ready;        /* the network flags of the last status row, for the keepalive */
};

static const char *aggregate_stage_name(enum aggregate_stage s)
{
    static const char *names[] = { "free", "validating", "setting-up", "running", "stopped" };
    return names[s];
}

/* Parse the intent, create the engine, take the locks, journal the intent and
 * start validation. -1 with err before anything is held. */
static int aggregate_begin(struct aggregate *a, const char *text, size_t len, struct client *client, uint64_t now, char *err, size_t errsz)
{
    struct daemon *d = &a->d;
    char name[64];
    memset(a, 0, sizeof *a);
    d->control_fd = -1; d->sequence = -1;
    a->start.in = a->start.out = a->start.err = -1;
    a->client = client;
    if (parse_intent(d, text, len, err, errsz)) return -1;
    snprintf(d->status_path, sizeof d->status_path, "%s/ffn-aggregate-%s-status.json", RUN, d->in.group);
    snprintf(d->intent_path, sizeof d->intent_path, "%s/ffn-aggregate-%s-intent.json", RUN, d->in.group);
    snprintf(d->lease_path, sizeof d->lease_path, "%s/ffn-aggregate-%s-lease.json", RUN, d->in.group);
    snprintf(d->pending_path, sizeof d->pending_path, "%s/ffn-aggregate-%s-network-pending.json", RUN, d->in.group);
    snprintf(d->dhcp_pid_path, sizeof d->dhcp_pid_path, "%s/ffn-aggregate-%s-dhcp.pid", RUN, d->in.group);
    process_start(getpid(), d->process_start, sizeof d->process_start);
    network_revision(d->network, d->in.lldp, d->applied_revision);
    snprintf(d->attempted_revision, sizeof d->attempted_revision, "%s", d->applied_revision);
    snprintf(d->lease_generation, sizeof d->lease_generation, "%s", d->applied_revision);
    offload_init(d);
    for (unsigned i = 0; i < d->in.count; i++) {
        d->native_members[i].front = d->in.sorted[i]; d->native_members[i].source = (uint32_t)front_source(d->in.sorted[i]);
        d->native_members[i].alias = d->off.enabled ? d->off.alias[i] : 0;
        d->fronts[i] = d->in.sorted[i];
    }
    d->front_count = d->in.count;
    if (engine_create(d, err, errsz)) goto failed;
    /* Legacy fabric owners take EX; scoped WAN/aggregate owners take SH and
     * individual port EX locks, allowing coexistence without overlapping RX. */
    if (take_lock(d, "ffn-fabric.lock", LOCK_SH, err, errsz)) goto failed;
    snprintf(name, sizeof name, "ffn-aggregate-%s.lock", d->in.group);
    if (take_lock(d, name, LOCK_EX, err, errsz)) goto failed;
    for (unsigned i = 0; i < d->in.count; i++) {
        snprintf(name, sizeof name, "ffn-aggregate-port-%u.lock", d->in.sorted[i]);
        if (take_lock(d, name, LOCK_EX, err, errsz)) goto failed;
    }
    /* Persist ownership before any netdevice mutation so the watchdog can
     * recover a crash during initialization as well as normal work. */
    if (write_intent_file(d, d->network, d->in.lldp, d->applied_revision)) { snprintf(err, errsz, "cannot write %s", d->intent_path); goto failed; }
    if (runtime_begin(&a->start, "validate", d->in.doc)) { snprintf(err, errsz, "validate helper: %s", strerror(errno)); unlink(d->intent_path); goto failed; }
    a->start_deadline = now + 120 * SECOND;
    a->stage = AGG_VALIDATING;
    d->started = d->last_input = now;
    d->next_status = d->next_lldp = d->next_fe100 = now;
    return 0;
failed:
    release_locks(d);
    if (d->engine) { ffn_lacp_destroy(d->engine); d->engine = NULL; }
    ffn_json_free(d->in.doc); d->in.doc = NULL;
    ffn_json_free(d->network); d->network = NULL;
    return -1;
}

/* The owner once set up: native owner, inspection, control socket. */
static int aggregate_open(struct aggregate *a, uint64_t now, char *err, size_t errsz)
{
    struct daemon *d = &a->d;
    if (!d->in.control_only) {
        char inspection_path[PATH];
        if (native_open(d, err, errsz)) return -1;
        snprintf(inspection_path, sizeof inspection_path, "%s/ffn-inspection-%s.json", RUN, d->in.group);
        if (inspection_open(d, inspection_path)) { snprintf(err, errsz, "%s", d->fatal); return -1; }
    }
    if (control_open(d, err, errsz)) return -1;
    d->last_input = now;   /* the lease clock starts when the owner serves */
    d->next_status = d->next_lldp = d->next_fe100 = now;
    a->stage = AGG_RUNNING;
    logmsg("%s serving: %u members, %s, %s", d->in.group, d->in.count, d->in.control_only ? "control only" : "native-c packet path",
           d->in.active ? "active" : "passive");
    return 0;
}

/* Drive the start helpers; the lease's first status row follows the open. */
static void aggregate_start_service(struct aggregate *a, uint64_t now)
{
    struct daemon *d = &a->d;
    struct ffn_json *reply, *request;
    char err[512];
    const char *verb = a->stage == AGG_VALIDATING ? "validate" : "network-setup";
    int status;
    bool closed = helper_service(&a->start);
    status = helper_status(&a->start);
    if (status < 0 || !closed) {
        if (now > a->start_deadline || (stop_signal && !shutting_down)) {
            helper_kill(&a->start); helper_service(&a->start); helper_close(&a->start);
            fail(d, "%s helper timed out", verb);
        }
        return;
    }
    reply = helper_reply(&a->start, status, verb, err, sizeof err);
    helper_close(&a->start);
    if (!reply) { fail(d, "%s", err); return; }
    if (a->stage == AGG_VALIDATING) {
        ffn_json_free(reply);
        if (d->in.control_only) { if (aggregate_open(a, now, err, sizeof err)) fail(d, "%s", err); return; }
        request = ffn_json_object();
        ffn_json_set(request, "intent", ffn_json_copy(d->in.doc));
        if (runtime_begin(&a->start, "network-setup", request)) { ffn_json_free(request); fail(d, "network-setup helper: %s", strerror(errno)); return; }
        ffn_json_free(request);
        a->start_deadline = now + 300 * SECOND;
        a->stage = AGG_SETTING_UP;
        return;
    }
    d->created = true;
    ffn_json_free(d->unit_status);
    d->unit_status = ffn_json_get(reply, "subinterfaces") ? ffn_json_copy(ffn_json_get(reply, "subinterfaces")) : ffn_json_array();
    ffn_json_free(reply);
    if (aggregate_open(a, now, err, sizeof err)) fail(d, "%s", err);
}

/* One pass of what the serve loop did between its polls. */
static void aggregate_tick(struct aggregate *a, uint64_t now)
{
    struct daemon *d = &a->d;
    bool network_current, network_ready, lease_ok;
    if (d->failed) return;
    if (a->stage != AGG_RUNNING) { aggregate_start_service(a, now); return; }   /* the helpers' deadlines bound the start */
    if (a->client) lease_lines(d, &a->client->in, now);
    if (d->failed) return;
    /* A stalled loop (a port attach waiting for the network lock, a close) could
     * not read the MP's observations meanwhile; the stall does not count against
     * their lease, which measures the MP's silence, not this process's. */
    if (a->last_tick && now > a->last_tick + SECOND) d->last_input += now - a->last_tick - SECOND;
    a->last_tick = now;
    /* An observation consumed this very tick stamps last_input later than `now`. */
    if (now > d->last_input && now - d->last_input > (d->sequence < 0 ? LEASE_BEFORE_FIRST : LEASE_AFTER_FIRST)) { fail(d, "MP/CP observation lease expired"); return; }
    ffn_lacp_tick(d->engine, now);
    lacp_service(d, now);
    if (ffn_lacp_fault(d->engine)) { fail(d, "%s", ffn_lacp_fault(d->engine)); return; }
    if (d->worker_running) worker_finish(d);
    configuration_service(d, now);
    children_service(d, now);
    if (d->native) inspection_tick(&d->insp, now);
    if (now >= d->next_lldp && d->in.lldp) { lldp_announce(d); d->next_lldp = now + LLDP_PERIOD; }
    network_current = !d->worker_running && !d->has_network_error && !strcmp(d->applied_revision, d->requested ? ffn_json_get_string(d->requested, "revision") : d->applied_revision);
    if (network_current && !d->in.control_only && network_dhcp(d->network) && ffn_lacp_distributing(d->engine, NULL, 0) && d->dhcp <= 0) dhcp_start(d);
    if (d->dhcp > 0 && d->dhcp_exited) { snprintf(d->network_error, sizeof d->network_error, "Aggregate DHCP client exited"); d->has_network_error = true; }
    network_current = network_current && !d->has_network_error;
    if (now >= d->next_status) { ffn_json_free(d->lease); d->lease = read_json(d->lease_path); }
    local_addresses(d);
    if (d->native) {
        int fault = ffn_packet_fault(d->native);
        if (fault > 0) { fail(d, "native owner fault: %s", strerror(fault)); return; }
        native_snapshot(d); datapath_sync(d, now, network_current);
    }
    if (d->failed) return;
    lease_ok = !network_dhcp(d->network) || (d->local_count && !(d->lease && ffn_json_get(d->lease, "error") && ffn_json_get(d->lease, "error")->type != FFN_JSON_NULL));
    network_ready = network_current && lease_ok;
    a->current = network_current; a->ready = network_ready;
    if (now >= d->next_status) { d->next_status = now + STATUS_PERIOD; status_row(d, now, network_current, network_ready, a->client ? &a->client->out : NULL); }
}

/* The trunk control socket's events from the daemon's poll. */
static void aggregate_control_events(struct aggregate *a, short revents, uint64_t now)
{
    if (a->stage != AGG_RUNNING || a->d.failed) return;
    if (revents & POLLIN) control_receive(&a->d, now);
    if (revents & (POLLHUP | POLLERR | POLLNVAL)) fail(&a->d, "control socket closed");
}

/* Withdraw everything; the error row reaches the lease when it is still open. */
static void aggregate_stop(struct aggregate *a)
{
    struct daemon *d = &a->d;
    if (d->failed && !d->stdin_closed && a->client) error_row(d, &a->client->out);
    if (a->start.running) { helper_kill(&a->start); helper_service(&a->start); }
    helper_close(&a->start);
    shutdown_owner(d);
    if (d->engine) { ffn_lacp_destroy(d->engine); d->engine = NULL; }
    ffn_json_free(d->in.doc); ffn_json_free(d->network); ffn_json_free(d->requested); ffn_json_free(d->worker_intent);
    ffn_json_free(d->unit_status); ffn_json_free(d->lease); ffn_json_free(d->unit_counts); ffn_json_free(d->workers);
    ffn_json_free(d->returns.rows); ffn_json_free(d->off.key_program);
    free(d->stdin_buffer.s);
    logmsg("%s stopped%s%s", d->in.group, d->failed ? ": " : "", d->failed ? d->fatal : "");
    a->stage = AGG_STOPPED;
}

/* ------------------------------------------------------- physical ports */
/* The owner of one faceplate port's packet path (ffn_wan_runtime.py and
 * ffn_physical_runtime.py before it): port 1 as the WAN owner `wan1` with the
 * fabric status file the policy provider reads, ports 2..24 as `physical-N`.
 * Copper ports 1..4 are faceplate ports like the others; what the copper VIF
 * driver did for them, carrier following the CP's PHY/MAC observation, is the
 * owner's link lease now: the MP timer delivers the faceplate rows through
 * `links`, and a port whose policy says `observed` keeps TAP carrier and
 * delivery only while a fresh observation says the link is up. */

#define LINKS_LEASE_DEFAULT 30
#define LINKS_LEASE_MAX 120

struct physical {
    unsigned port, source;
    char owner[16], tap[8], unit[48];
    char intent_path[PATH], state_path[PATH], inspection_path[PATH];
    bool observed;                 /* carrier policy: observed (lease) or always */
    bool carrier_up;
    char carrier_reason[160];
    long long sequence; double age;
    struct ffn_json *observation;  /* the port's last faceplate row */
    int ifindex;
    uint64_t next_status;
    struct ffn_json *state;        /* the published status document */
};

static const char *COPPER_PROFILE = "/etc/ffn/vif-copper.json", *CARRIER_POLICY = "/etc/ffn/port-carrier.json";
static const char *LINKS_FILE = "ffn-port-links.json";

/* The commissioned copper wiring may be corrected by copper-identify; the
 * profile then names the switch port behind a front port 1..4. */
static int physical_source(unsigned port)
{
    static const int copper[5] = { 0, 28, 13, 14, 15 };
    struct ffn_json *profile, *ports, *entry;
    long long bcm;
    char key[8];
    int source = port >= 1 && port <= 4 ? copper[port] : front_source(port);
    if (port > 4) return source;
    profile = read_json(COPPER_PROFILE);
    if (!profile) return source;
    snprintf(key, sizeof key, "%u", port);
    ports = ffn_json_get(profile, "ports");
    entry = ports ? ffn_json_get(ports, key) : NULL;
    if (entry && ffn_json_get_int(entry, "bcm_port", &bcm) && (bcm == 28 || bcm == 13 || bcm == 14 || bcm == 15)) source = (int)bcm;
    ffn_json_free(profile);
    return source;
}

static bool physical_observed_policy(unsigned port)
{
    struct ffn_json *policy = read_json(CARRIER_POLICY);
    const char *value = NULL;
    char key[8];
    bool observed = port >= 2 && port <= 4;   /* the copper ports the VIF driver gated */
    if (!policy) return observed;
    snprintf(key, sizeof key, "%u", port);
    value = ffn_json_get_string(policy, key);
    if (!value) value = ffn_json_get_string(policy, "default");
    if (value) observed = !strcmp(value, "observed");
    ffn_json_free(policy);
    return observed;
}

static int physical_select(struct physical *p, unsigned port)
{
    memset(p, 0, sizeof *p);
    if (port < 1 || port > 24) return -1;
    p->port = port;
    p->source = (unsigned)physical_source(port);
    snprintf(p->tap, sizeof p->tap, "p%u", port);
    if (port == 1) {
        snprintf(p->owner, sizeof p->owner, "wan1");
        snprintf(p->unit, sizeof p->unit, "ffn-wan-attachment.service");
        snprintf(p->intent_path, sizeof p->intent_path, "%s/ffn-wan-attachment.json", RUN);
        snprintf(p->state_path, sizeof p->state_path, "%s/ffn-fabric.json", RUN);
        p->inspection_path[0] = 0;   /* the shared /run/ffn-inspection.json */
    } else {
        snprintf(p->owner, sizeof p->owner, "physical-%u", port);
        snprintf(p->unit, sizeof p->unit, "ffn-physical@%u.service", port);
        snprintf(p->intent_path, sizeof p->intent_path, "%s/ffn-physical-%u-intent.json", RUN, port);
        snprintf(p->state_path, sizeof p->state_path, "%s/ffn-physical-%u-status.json", RUN, port);
        snprintf(p->inspection_path, sizeof p->inspection_path, "%s/ffn-inspection-physical-%u.json", RUN, port);
    }
    p->observed = physical_observed_policy(port);
    p->sequence = -1;
    return 0;
}

/* ffn_wan_runtime.status(): the published document is evidence only while
 * its process is alive, unchanged and publishing. */
static struct ffn_json *physical_status(const struct physical *p)
{
    struct ffn_json *result = ffn_json_object(), *value = read_json(p->state_path);
    char current_boot[40], start[32];
    const char *owner, *boot, *recorded;
    long long pid = 0;
    double updated = 0;
    bool running = false;
    boot_id(current_boot, sizeof current_boot);
    if (value && value->type == FFN_JSON_OBJECT) {
        owner = ffn_json_get_string(value, "owner"); boot = ffn_json_get_string(value, "boot_id");
        recorded = ffn_json_get_string(value, "process_start");
        ffn_json_get_int(value, "pid", &pid); ffn_json_get_number(value, "updated_monotonic", &updated);
        if (owner && boot && recorded && !strcmp(owner, p->owner) && !strcmp(boot, current_boot) && pid > 0 &&
            process_start((pid_t)pid, start, sizeof start) == 0 && !strcmp(start, recorded)) {
            double age = (double)now_us() / 1e6 - updated;
            char path[64], *text; size_t len;
            bool zombie = false;
            snprintf(path, sizeof path, "/proc/%lld/stat", pid);
            if (read_file(path, &text, &len) == 0) { char *q = strrchr(text, ')'); zombie = q && q[2] == 'Z'; free(text); }
            running = !zombie && age >= 0 && age <= 5;
        }
    }
    ffn_json_set(result, "running", ffn_json_bool(running));
    ffn_json_set(result, "boot_id", ffn_json_string(current_boot));
    ffn_json_set(result, "attachment", running ? ffn_json_copy(value) : ffn_json_object());
    ffn_json_set(result, "hardware_offload", ffn_json_bool(false));
    ffn_json_free(value);
    return result;
}

/* The TAP's ifindex inside the data namespace, as the policy bindings compare it. */
static int tap_ifindex(const char *tap)
{
    struct helper h;
    char *const argv[] = { "/usr/sbin/ip", "-n", (char *)NS, "-j", "link", "show", "dev", (char *)tap, NULL };
    struct ffn_json *doc = NULL;
    char perr[64];
    long long index = -1;
    if (helper_run(&h, argv, NULL, 15 * SECOND) == 0 && h.output.len) doc = ffn_json_parse(h.output.s, h.output.len, perr, sizeof perr);
    if (doc && doc->type == FFN_JSON_ARRAY && doc->count == 1) ffn_json_get_int(doc->items[0], "ifindex", &index);
    ffn_json_free(doc);
    helper_close(&h);
    return (int)index;
}

/* The link observation the MP timer delivered, judged for this port. */
static void physical_carrier(struct daemon *d, struct physical *p, uint64_t now)
{
    char path[PATH], key[8], reason[160] = "";
    struct ffn_json *doc, *ports, *row = NULL;
    double received = 0, age = 0, lease = LINKS_LEASE_DEFAULT;
    long long sequence = -1;
    bool up = false, fresh = false;
    if (!p->observed) return;
    snprintf(path, sizeof path, "%s/%s", RUN, LINKS_FILE);
    doc = read_json(path);
    ffn_json_free(p->observation); p->observation = NULL;
    if (doc) {
        ffn_json_get_number(doc, "received_monotonic", &received); ffn_json_get_number(doc, "age_seconds", &age);
        ffn_json_get_number(doc, "lease_seconds", &lease); ffn_json_get_int(doc, "sequence", &sequence);
        ports = ffn_json_get(doc, "ports");
        snprintf(key, sizeof key, "%u", p->port);
        row = ports ? ffn_json_get(ports, key) : NULL;
        fresh = (double)now / 1e6 - received >= 0 && (double)now / 1e6 - received < lease - age;
        if (row) p->observation = ffn_json_copy(row);
        p->sequence = sequence; p->age = (double)now / 1e6 - received + age;
    }
    if (!doc) snprintf(reason, sizeof reason, "no link observation delivered");
    else if (!fresh) snprintf(reason, sizeof reason, "link observation stale");
    else if (!row) snprintf(reason, sizeof reason, "port absent from the link observation");
    else {
        /* ffn_copper_vif.status(): every stage of the copper path must agree. */
        static const char *copper_required[] = { "available", "enabled", "phy_enabled", "mac_enabled", "link", "mac_link", "datapath_link" };
        static const char *optical_required[] = { "available", "enabled", "link" };
        const char *media = ffn_json_get_string(row, "media");
        bool copper = (media && !strcmp(media, "copper")) || p->port <= 4;
        const char **required = copper ? copper_required : optical_required;
        unsigned count = copper ? 7 : 3;
        bool pending = false, ok = true;
        long long speed = -1, mac_speed = -1;
        for (unsigned i = 0; i < count && ok; i++) { bool v = false; if (!ffn_json_get_bool(row, required[i], &v) || !v) { ok = false; snprintf(reason, sizeof reason, "%s is not true in the observation", required[i]); } }
        if (ok && copper && ffn_json_get_bool(row, "phy_pending", &pending) && pending) { ok = false; snprintf(reason, sizeof reason, "PHY configuration pending or unknown"); }
        if (ok && copper && ffn_json_get_int(row, "speed_mbps", &speed) && ffn_json_get_int(row, "mac_speed_mbps", &mac_speed) && speed != mac_speed) {
            ok = false; snprintf(reason, sizeof reason, "PHY and MAC speeds are not synchronized");
        }
        if (ok) { up = true; snprintf(reason, sizeof reason, "ready"); }
    }
    ffn_json_free(doc);
    if (up != p->carrier_up || strcmp(reason, p->carrier_reason)) {
        if (up != p->carrier_up) {
            if (ffn_packet_carrier(d->native, up)) { fail(d, "carrier %s: %s", up ? "up" : "down", strerror(errno)); return; }
            logmsg("%s carrier %s: %s", p->tap, up ? "up" : "down", reason);
        }
        p->carrier_up = up;
        snprintf(p->carrier_reason, sizeof p->carrier_reason, "%s", reason);
    }
}

/* The parent's static addresses from the network owner's journal, for local-service classification. */
static void physical_addresses(struct daemon *d, struct physical *p)
{
    struct ffn_json *doc = read_json(NETWORK_JOURNAL), *ports, *port, *addresses;
    uint8_t v4[FFN_PACKET_LOCAL_MAX][4], v6[FFN_PACKET_LOCAL_MAX][16];
    unsigned n4 = 0, n6 = 0;
    bool inspect = inspection_port(&d->insp, p->port);
    ports = doc ? ffn_json_get(doc, "ports") : NULL;
    port = ports ? ffn_json_get(ports, p->tap) : NULL;
    addresses = port ? ffn_json_get(port, "addresses") : NULL;
    for (size_t i = 0; addresses && addresses->type == FFN_JSON_ARRAY && i < addresses->count; i++) {
        const char *a = addresses->items[i]->type == FFN_JSON_STRING ? addresses->items[i]->string : NULL;
        char host[64];
        if (!a) continue;
        snprintf(host, sizeof host, "%.*s", (int)strcspn(a, "/"), a);
        if (n4 < FFN_PACKET_LOCAL_MAX && inet_pton(AF_INET, host, v4[n4]) == 1) n4++;
        else if (n6 < FFN_PACKET_LOCAL_MAX && inet_pton(AF_INET6, host, v6[n6]) == 1) n6++;
    }
    ffn_json_free(doc);
    if (ffn_packet_configure(d->native, (const uint8_t *)v4, n4, (const uint8_t *)v6, n6, inspect ? d->insp.handle : NULL,
                             inspect ? (ffn_scan_fn)d->insp.scan : NULL))
        fail(d, "native configure: %s", strerror(errno));
}

static void physical_publish(struct daemon *d, struct physical *p, uint64_t now)
{
    struct ffn_json *state = p->state, *counters = ffn_json_object(), *carrier = ffn_json_object();
    struct timespec ts;
    uint64_t cstats[2] = { 0, 1 };
    char key[16];
    if (ffn_packet_counters(d->native, d->counters, FFN_PACKET_COUNTERS)) { fail(d, "native counters: %s", strerror(errno)); return; }
    for (unsigned i = 0; i < FFN_PACKET_COUNTERS; i++) {
        const char *name = COUNTER_NAMES[i];
        if (i == 0) { snprintf(key, sizeof key, "rx_%s", p->tap); name = key; }
        if (i == 1) { snprintf(key, sizeof key, "tx_%s", p->tap); name = key; }
        ffn_json_set(counters, name, ffn_json_int((long long)d->counters[i]));
    }
    ffn_packet_carrier_stats(d->native, cstats, 2);
    ffn_json_set(counters, "carrier_drop", ffn_json_int((long long)cstats[0]));
    for (unsigned i = 0; i < 6; i++) {
        ffn_json_set(d->insp.counts, VERDICTS[i], ffn_json_int((long long)d->counters[8 + i]));
        if (i) { snprintf(key, sizeof key, "port_%u_%s", p->port, VERDICTS[i]); ffn_json_set(d->insp.counts, key, ffn_json_int((long long)d->counters[8 + i])); }
    }
    ffn_json_set(state, "counters", counters);
    if (d->returns.active && d->returns.rows) {
        struct ffn_json *r = ffn_json_object();
        ffn_json_set(r, "rows", ffn_json_copy(d->returns.rows)); ffn_json_set(r, "deadline_ms", ffn_json_int((long long)d->returns.deadline_ms));
        ffn_json_set(state, "fe100_return", r);
    } else ffn_json_set(state, "fe100_return", ffn_json_null());
    ffn_json_set(state, "workers", workers_json(d));
    ffn_json_set(carrier, "policy", ffn_json_string(p->observed ? "observed" : "always"));
    ffn_json_set(carrier, "up", ffn_json_bool(p->observed ? p->carrier_up : true));
    ffn_json_set(carrier, "reason", ffn_json_string(p->observed ? p->carrier_reason : "carrier follows the switch port"));
    ffn_json_set(carrier, "sequence", ffn_json_int(p->sequence));
    ffn_json_set(carrier, "age_seconds", ffn_json_number(p->age));
    ffn_json_set(carrier, "observation", p->observation ? ffn_json_copy(p->observation) : ffn_json_null());
    ffn_json_set(state, "carrier", carrier);
    clock_gettime(CLOCK_REALTIME, &ts);
    ffn_json_set(state, "updated_at", ffn_json_number((double)ts.tv_sec + (double)ts.tv_nsec / 1e9));
    ffn_json_set(state, "updated_monotonic", ffn_json_number((double)now / 1e6));
    write_json(p->state_path, state);
}

/* A faceplate port in the daemon's table: what the MP wants it to be, what
 * the daemon holds for it, and why not when the two differ. */
enum port_mode { MODE_DISABLED, MODE_L3, MODE_AGGREGATE };

struct port {
    unsigned number;
    enum port_mode desired;
    char group[8];              /* MODE_AGGREGATE: the group the row names */
    bool attached;              /* an l3 owner runs on the TAP */
    struct daemon d;
    struct physical p;
    char reason[160];           /* why the port is not as desired, or "running" */
    uint64_t next_try, next_tick;
    unsigned failures;
    struct aggregate *member_of;  /* the live aggregate lease holding this port */
    bool remake_asked;          /* this attachment asked the network owner once to remake a plain TAP */
};

static const char *port_mode_name(enum port_mode m)
{
    static const char *names[] = { "disabled", "l3", "aggregate" };
    return names[m];
}

/* Attach the port's packet owner: what `physical serve` did before its loop.
 * -1 with err and nothing held on failure; the table retries it. */
static int port_attach(struct port *port, char *err, size_t errsz)
{
    struct daemon *d = &port->d;
    struct physical *p = &port->p;
    char current_boot[40], lock_name[64], path[PATH];
    int network_lock = -1;
    memset(d, 0, sizeof *d);
    d->control_fd = -1;
    if (physical_select(p, port->number)) { snprintf(err, errsz, "invalid physical port %u", port->number); return -1; }
    if (boot_id(current_boot, sizeof current_boot)) { snprintf(err, errsz, "boot identity unreadable"); return -1; }
    snprintf(lock_name, sizeof lock_name, "ffn-aggregate-port-%u.lock", port->number);
    if (take_lock(d, "ffn-fabric.lock", LOCK_SH, err, errsz) || take_lock(d, lock_name, LOCK_EX, err, errsz)) { release_locks(d); return -1; }
    d->fronts[0] = (uint16_t)port->number; d->front_count = 1;
    /* The network owner reconfigures the TAP under this lock; attach while it is quiet. */
    snprintf(path, sizeof path, "%s/ffn-network.lock", RUN);
    network_lock = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (network_lock < 0 || lock_wait(network_lock, LOCK_EX, LOCK_WAIT)) { snprintf(err, errsz, "network lock: %s", strerror(errno)); goto failed; }
    d->native = ffn_packet_open(TRUNK, NS, p->tap, p->source);
    if (!d->native) { snprintf(err, errsz, "native physical owner: %s", strerror(errno)); goto failed; }
    if (native_start(d, p->tap, err, errsz)) goto failed;
    p->ifindex = tap_ifindex(p->tap);
    if (p->ifindex < 0) { snprintf(err, errsz, "%s: no TAP in %s", p->tap, NS); goto failed; }
    p->state = ffn_json_object();
    ffn_json_set(p->state, "owner", ffn_json_string(p->owner));
    ffn_json_set(p->state, "pid", ffn_json_int(getpid()));
    ffn_json_set(p->state, "boot_id", ffn_json_string(current_boot));
    process_start(getpid(), d->process_start, sizeof d->process_start);
    ffn_json_set(p->state, "process_start", ffn_json_string(d->process_start));
    { struct ffn_json *ports = ffn_json_array(); ffn_json_push(ports, ffn_json_int(port->number)); ffn_json_set(p->state, "ports", ports); }
    ffn_json_set(p->state, "max_mtu", ffn_json_int(1500));
    ffn_json_set(p->state, "transport", ffn_json_string("direct OCTEON physical port"));
    ffn_json_set(p->state, "packet_execution", ffn_json_string("native-c"));
    ffn_json_set(p->state, "hardware_offload", ffn_json_bool(false));
    ffn_json_set(p->state, "owner_daemon", ffn_json_string("ffn-portd"));
    { struct ffn_json *interfaces = ffn_json_object(); ffn_json_set(interfaces, p->tap, ffn_json_int(p->ifindex)); ffn_json_set(p->state, "interfaces", interfaces); }
    write_json(p->state_path, p->state);
    flock(network_lock, LOCK_UN); close(network_lock); network_lock = -1;
    if (inspection_open(d, p->inspection_path[0] ? p->inspection_path : NULL)) { snprintf(err, errsz, "%.200s", d->fatal); goto failed; }
    if (p->observed && ffn_packet_carrier(d->native, 0)) { snprintf(err, errsz, "carrier: %s", strerror(errno)); goto failed; }
    snprintf(p->carrier_reason, sizeof p->carrier_reason, "no link observation delivered");
    port->attached = true;
    port->remake_asked = false;
    port->next_tick = now_us();
    logmsg("%s serving %s (%s, source %u, carrier %s, %u of %u lanes%s)", p->owner, p->tap, port->number == 1 ? "WAN" : "physical", p->source,
           p->observed ? "observed" : "always", d->lanes, d->lanes_wanted, ffn_packet_multiqueue(d->native) ? "" : ", TAP without multi_queue");
    return 0;
failed:
    if (network_lock >= 0) close(network_lock);
    if (d->insp.counts) inspection_close(&d->insp);
    if (d->native) native_close(d);
    unlink(p->state_path);
    release_locks(d);
    ffn_json_free(p->state); p->state = NULL;
    ffn_json_free(p->observation); p->observation = NULL;
    return -1;
}

/* Release the port's packet owner: what `physical serve` did when it stopped. */
static void port_detach(struct port *port)
{
    struct daemon *d = &port->d;
    struct physical *p = &port->p;
    if (!port->attached) return;
    if (d->failed) logmsg("%s stopping: %s", p->owner, d->fatal);
    if (d->native) native_close(d);
    if (d->insp.counts) inspection_close(&d->insp);
    unlink(p->state_path);
    release_locks(d);
    ffn_json_free(p->state); p->state = NULL;
    ffn_json_free(p->observation); p->observation = NULL;
    ffn_json_free(d->returns.rows); d->returns.rows = NULL;
    ffn_json_free(d->workers); d->workers = NULL;
    port->attached = false;
    logmsg("%s stopped", p->owner);
}

/* The TAP this owner attached to was made without multi_queue, so the library
 * could open one queue of it while the lane policy asks for more. The network
 * owner (core ffn_linux_network.py, behind the platform adapter) is the one
 * that makes and remakes physical TAPs: `reconcile --port pN` deletes the
 * device and makes it again with multi_queue under the network lock, keeping
 * its MAC, addresses and routes. This owner's queues then belong to a device
 * that is gone; port_tick notices, the table re-attaches the port, and the
 * new device takes a queue per lane. Asked once per attachment: a refusal is
 * recorded in the port's reason and the port keeps serving on its one lane. */
static void tap_remake_request(struct port *port)
{
    struct daemon *d = &port->d;
    struct physical *p = &port->p;
    struct helper h;
    char line[200];
    char *const argv[] = { (char *)PYTHON, (char *)NETWORK_OWNER, "reconcile", "--port", p->tap, NULL };
    int status;
    port->remake_asked = true;
    logmsg("%s: %s was made without multi_queue and the policy wants %u lanes; asking the network owner to remake it", p->owner, p->tap, d->lanes_wanted);
    status = helper_run(&h, argv, NULL, 60 * SECOND);
    if (status == 0) {
        /* {"reconciled": true, "ports": {"pN": {"remade": ..., "routes_restored": n, "addresses_restored": n}}} */
        struct ffn_json *reply = h.output.len ? ffn_json_parse(h.output.s, h.output.len, line, sizeof line) : NULL, *ports, *report;
        char *text = NULL;
        ports = reply ? ffn_json_get(reply, "ports") : NULL;
        report = ports ? ffn_json_get(ports, p->tap) : NULL;
        if (report) text = ffn_json_dump(report);
        logmsg("%s: network owner reconciled %s: %.160s", p->owner, p->tap, text ? text : "no report in the reply");
        free(text); ffn_json_free(reply);
    } else {
        last_line(&h.error, line, sizeof line);
        if (!line[0]) last_line(&h.output, line, sizeof line);
        logmsg("%s: network owner did not remake %s: %s", p->owner, p->tap, line[0] ? line : status < 0 ? "timed out" : "failed");
        snprintf(port->reason, sizeof port->reason, "running with %u of %u lanes; the network owner did not remake %s: %.80s", d->lanes, d->lanes_wanted, p->tap, line[0] ? line : "failed");
    }
    helper_close(&h);
}

/* One second of an attached port: what `physical serve` did per loop. A
 * worker fault or a failed native call marks the owner failed; the table
 * detaches and re-attaches it. */
static void port_tick(struct port *port, uint64_t now)
{
    struct daemon *d = &port->d;
    struct physical *p = &port->p;
    int fault;
    if (!port->attached || d->failed) return;
    fault = ffn_packet_fault(d->native);
    if (fault > 0) { fail(d, "native owner fault: %s", strerror(fault)); return; }
    if (now < port->next_tick) return;
    port->next_tick = now + STATUS_PERIOD;
    /* The queues belong to the device opened; a port without traffic would
     * never learn from its workers that the network owner remade it. */
    if (ffn_packet_tap_check(d->native)) { fail(d, "TAP %s %s", p->tap, errno == ENODEV ? "is gone" : "was replaced"); return; }
    if (!port->remake_asked && d->lanes < d->lanes_wanted && !ffn_packet_multiqueue(d->native)) {
        tap_remake_request(port);
        if (ffn_packet_tap_check(d->native)) { fail(d, "TAP %s %s", p->tap, errno == ENODEV ? "is gone" : "was replaced"); return; }
    }
    /* Quiesce the workers before the inspection engine or the return
     * bindings change under them; resume only after validated state. */
    if (ffn_packet_pause(d->native)) { fail(d, "native pause: %s", strerror(errno)); return; }
    inspection_tick(&d->insp, now);
    physical_addresses(d, p);
    fe100_renew(d, now);
    physical_carrier(d, p, now);
    if (ffn_packet_resume(d->native)) { fail(d, "native resume: %s", strerror(errno)); return; }
    if (d->failed) return;
    physical_publish(d, p, now);
}

/* The same delivery in the form the route tracker reads: {"boot_id": this DP
 * boot, "revision": the network journal's, "links": {"pN": up}, "observed":
 * CLOCK_MONOTONIC seconds}. octeon/debian/ffn_network.py route_link judges a
 * link-tracked route by it and the core tracker (ffn_static_routes.py)
 * withdraws the route once it is older than 60 s or from another DP boot. A
 * port is up for a route when its row says available, enabled and link: the
 * rule the MP feed applies (management/daemon_backend.py, route-links
 * refresh, through the network owner's `health`). That feed alone wrote this
 * file until 2026-10-20, when a core patch install stopped it with the control
 * daemon, nothing started it again, and the tracker withdrew both default
 * routes for fifteen minutes. The observation the owners receive every three
 * seconds anyway now carries it, so the tracker no longer depends on the MP
 * feed; the feed remains a second writer of the same record. */
static int route_links_record(const struct ffn_json *byport)
{
    struct ffn_json *out, *links, *network;
    char boot[40];
    long long revision;
    int rc;
    if (boot_id(boot, sizeof boot)) return -1;
    out = ffn_json_object(); links = ffn_json_object();
    for (size_t i = 0; i < byport->count; i++) {
        const struct ffn_json *row = byport->items[i];
        bool available = false, enabled = false, link = false;
        char key[12];
        ffn_json_get_bool(row, "available", &available); ffn_json_get_bool(row, "enabled", &enabled); ffn_json_get_bool(row, "link", &link);
        snprintf(key, sizeof key, "p%s", byport->keys[i]);
        ffn_json_set(links, key, ffn_json_bool(available && enabled && link));
    }
    network = read_json(NETWORK_JOURNAL);
    ffn_json_set(out, "boot_id", ffn_json_string(boot));
    ffn_json_set(out, "revision", network && ffn_json_get_int(network, "revision", &revision) ? ffn_json_int(revision) : ffn_json_null());
    ffn_json_set(out, "links", links);
    ffn_json_set(out, "observed", ffn_json_number((double)now_us() / 1e6));
    rc = write_json(ROUTE_LINKS, out);
    ffn_json_free(network); ffn_json_free(out);
    return rc;
}

/* ------------------------------------------------------ socket clients */
/* The daemon's control socket, as its command lines reach it. */

static int daemon_connect(void)
{
    struct sockaddr_un addr;
    int fd;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (snprintf(addr.sun_path, sizeof addr.sun_path, "%s/%s", RUN, SOCKET_NAME) >= (int)sizeof addr.sun_path) { errno = ENAMETOOLONG; return -1; }
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr)) { int e = errno; close(fd); errno = e; return -1; }
    return fd;
}

/* 1 when a reply line is an error row: its top-level object carries "error"
 * (a nested counter named so, as the workers' is, is not one). */
static int reply_is_error(const char *line)
{
    char perr[64];
    struct ffn_json *doc = ffn_json_parse(line, strlen(line), perr, sizeof perr);
    int rc = !doc || doc->type != FFN_JSON_OBJECT || ffn_json_get(doc, "error") != NULL;
    ffn_json_free(doc);
    return rc;
}

/* Send one request line, read one reply line (bounded) within timeout_ms. */
static int daemon_request(int fd, const char *line, size_t len, struct text *reply, int timeout_ms)
{
    uint64_t deadline = now_us() + (uint64_t)timeout_ms * 1000;
    char buf[4096];
    for (size_t off = 0; off < len;) {
        ssize_t w = write(fd, line + off, len - off);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) return -1;
        off += (size_t)w;
    }
    if (line[len - 1] != '\n' && write(fd, "\n", 1) != 1) return -1;
    for (;;) {
        struct pollfd pfd = { fd, POLLIN, 0 };
        uint64_t now = now_us();
        ssize_t n;
        if (reply->s && memchr(reply->s, '\n', reply->len)) return 0;
        if (now >= deadline) { errno = ETIMEDOUT; return -1; }
        if (poll(&pfd, 1, (int)((deadline - now) / 1000)) < 0 && errno != EINTR) return -1;
        n = read(fd, buf, sizeof buf);
        if (n == 0) { errno = ECONNRESET; return -1; }
        if (n < 0) { if (errno == EINTR || errno == EAGAIN) continue; return -1; }
        text_put(reply, buf, (size_t)n);
        if (reply->len > 1 << 20) { errno = EMSGSIZE; return -1; }
    }
}

/* ------------------------------------------------------------- links */
/* The MP timer's faceplate observation for every physical owner.
 * {"mp_boot_id", "sequence", "age_seconds", "lease_seconds"?, "ports": [rows]}
 * becomes /run/ffn-port-links.json with the receipt time; an observation
 * that is not newer than the recorded one is refused, so a delayed or
 * replayed delivery cannot revive a carrier. The accepted rows are also
 * recorded for the route tracker (route_links_record); the reply says
 * whether that record was written, so the MP's journal shows it. The daemon
 * records a delivery that reaches its socket; the command line records one
 * itself when the daemon does not answer, so the tracker's observation never
 * waits for the daemon. */
static int links_record_doc(const struct ffn_json *doc, char *reply, size_t replysz)
{
    char path[PATH];
    struct ffn_json *ports, *previous, *out, *byport;
    const char *mp_boot;
    long long sequence, lease = LINKS_LEASE_DEFAULT, previous_sequence = -1;
    double age;
    unsigned count = 0;
    bool route_links;
    mp_boot = doc ? ffn_json_get_string(doc, "mp_boot_id") : NULL;
    ports = doc ? ffn_json_get(doc, "ports") : NULL;
    if (!doc || doc->type != FFN_JSON_OBJECT || !mp_boot || !ffn_json_get_int(doc, "sequence", &sequence) || sequence < 0 ||
        !ffn_json_get_number(doc, "age_seconds", &age) || !(age >= 0 && age < 60) || !ports || ports->type != FFN_JSON_ARRAY || ports->count > 24 ||
        (ffn_json_get(doc, "lease_seconds") && (!ffn_json_get_int(doc, "lease_seconds", &lease) || lease < 1 || lease > LINKS_LEASE_MAX))) {
        snprintf(reply, replysz, "{\"error\": \"invalid link observation\"}"); return 1;
    }
    snprintf(path, sizeof path, "%s/%s", RUN, LINKS_FILE);
    previous = read_json(path);
    if (previous) {
        const char *old_boot = ffn_json_get_string(previous, "mp_boot_id");
        if (old_boot && !strcmp(old_boot, mp_boot)) ffn_json_get_int(previous, "sequence", &previous_sequence);
        ffn_json_free(previous);
    }
    if (sequence <= previous_sequence) { snprintf(reply, replysz, "{\"error\": \"stale link observation\"}"); return 1; }
    out = ffn_json_object(); byport = ffn_json_object();
    for (size_t i = 0; i < ports->count; i++) {
        struct ffn_json *row = ports->items[i];
        long long port;
        char key[8];
        if (row->type != FFN_JSON_OBJECT || !ffn_json_get_int(row, "port", &port) || port < 1 || port > 24) continue;
        snprintf(key, sizeof key, "%lld", port);
        if (ffn_json_get(byport, key)) continue;
        ffn_json_set(byport, key, ffn_json_copy(row));
        count++;
    }
    ffn_json_set(out, "mp_boot_id", ffn_json_string(mp_boot));
    ffn_json_set(out, "sequence", ffn_json_int(sequence));
    ffn_json_set(out, "received_monotonic", ffn_json_number((double)now_us() / 1e6));
    ffn_json_set(out, "age_seconds", ffn_json_number(age));
    ffn_json_set(out, "lease_seconds", ffn_json_int(lease));
    ffn_json_set(out, "ports", byport);
    if (write_json(path, out)) { snprintf(reply, replysz, "{\"error\": \"cannot record the link observation\"}"); ffn_json_free(out); return 1; }
    route_links = route_links_record(byport) == 0;
    snprintf(reply, replysz, "{\"accepted\": true, \"ports\": %u, \"lease_seconds\": %lld, \"sequence\": %lld, \"route_links\": %s}",
             count, lease, sequence, route_links ? "true" : "false");
    ffn_json_free(out);
    return 0;
}

/* `links`: stdin to the daemon's socket, or recorded here when it is away. */
static int links_tool(void)
{
    struct text t = { 0 }, reply = { 0 };
    char buf[4096], perr[64], line[256];
    ssize_t n;
    struct ffn_json *doc;
    int fd, rc;
    while ((n = read(0, buf, sizeof buf)) > 0) text_put(&t, buf, (size_t)n);
    doc = ffn_json_parse(t.s ? t.s : "", t.len, perr, sizeof perr);
    if (!doc || doc->type != FFN_JSON_OBJECT) { printf("{\"error\": \"invalid link observation\"}\n"); ffn_json_free(doc); free(t.s); return 1; }
    ffn_json_set(doc, "op", ffn_json_string("links"));
    fd = daemon_connect();
    if (fd >= 0) {
        char *text = ffn_json_dump(doc);
        rc = text ? daemon_request(fd, text, strlen(text), &reply, 10000) : -1;
        free(text); close(fd);
        if (rc == 0) {
            char *nl = memchr(reply.s, '\n', reply.len);
            if (nl) *nl = 0;
            puts(reply.s);
            rc = reply_is_error(reply.s);
            free(reply.s); free(t.s); ffn_json_free(doc);
            return rc;
        }
        free(reply.s);
    }
    ffn_json_remove(doc, "op");
    rc = links_record_doc(doc, line, sizeof line);
    puts(line);
    ffn_json_free(doc); free(t.s);
    return rc;
}

/* -------------------------------------------------------- the watchdog */
/* `sweep` (the dataplane watchdog timer) and `recover` (the MP, with the
 * owner's identity): retire an owner that stopped reporting or admits a silent
 * controller, then reclaim its TAP and run files through the teardown helper.
 * The decisions live here; the kernel transaction stays one implementation. */

static int try_lock(const char *path)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    if (flock(fd, LOCK_EX | LOCK_NB)) { close(fd); return -1; }
    return fd;
}

/* The process's command line, NUL separated; -1 when the process is gone. */
static int cmdline_read(pid_t pid, char **text, size_t *len)
{
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/cmdline", (int)pid);
    return read_file(path, text, len);
}

/* Whether an argument ends with `tail` (the owner binaries) or, basename_only,
 * is exactly `tail` (udhcpc). */
static bool args_have(const char *text, size_t len, const char *tail, bool basename_only)
{
    size_t want = strlen(tail);
    for (size_t i = 0; i < len; i += strlen(text + i) + 1) {
        const char *arg = text + i, *base = strrchr(arg, '/');
        size_t n = strlen(arg);
        if (basename_only ? !strcmp(base ? base + 1 : arg, tail) : (n >= want && !strcmp(arg + n - want, tail))) return true;
    }
    return false;
}

static bool owner_cmdline(pid_t pid)
{
    char *text; size_t len;
    bool found;
    if (cmdline_read(pid, &text, &len)) return false;
    found = args_have(text, len, "ffn-portd", false) || args_have(text, len, "ffn-aggregated", false) || args_have(text, len, "/ffn_aggregate_runtime.py", false);
    free(text);
    return found;
}

/* `ffn-portd serve`: the daemon hosting every port and every group. */
static bool serve_cmdline(pid_t pid)
{
    char *text; size_t len;
    bool found;
    if (cmdline_read(pid, &text, &len)) return false;
    found = args_have(text, len, "ffn-portd", false) && args_have(text, len, "serve", true);
    free(text);
    return found;
}

/* 0 when the group is recovered or there is nothing to do; -1 with err when
 * the owner is alive and healthy (left alone), its identity is not what the
 * journal says, or the cleanup failed. */
static int recover_group(const char *group, const char *token, char *err, size_t errsz)
{
    char intent_path[PATH], status_path[PATH], lock_path[PATH], dhcp_path[PATH], current_boot[40], start[32];
    struct ffn_json *intent, *status = NULL, *again = NULL, *request, *reply, *network, *management;
    const char *intent_token, *intent_boot;
    int lock, rc = -1;
    snprintf(intent_path, sizeof intent_path, "%s/ffn-aggregate-%s-intent.json", RUN, group);
    snprintf(status_path, sizeof status_path, "%s/ffn-aggregate-%s-status.json", RUN, group);
    snprintf(lock_path, sizeof lock_path, "%s/ffn-aggregate-%s.lock", RUN, group);
    snprintf(dhcp_path, sizeof dhcp_path, "%s/ffn-aggregate-%s-dhcp.pid", RUN, group);
    intent = read_json(intent_path);
    if (!intent) {
        if (errno == ENOENT) return 0; /* nothing journaled: nothing to recover */
        snprintf(err, errsz, "intent unreadable"); return -1;
    }
    intent_token = ffn_json_get_string(intent, "token"); intent_boot = ffn_json_get_string(intent, "boot_id");
    if (!intent_token || !intent_boot || boot_id(current_boot, sizeof current_boot) || strcmp(intent_boot, current_boot) ||
        (token && strcmp(token, intent_token))) {
        snprintf(err, errsz, "Aggregate recovery identity changed"); ffn_json_free(intent); return -1;
    }
    lock = try_lock(lock_path);
    if (lock < 0) {
        /* An alive owner must be stopped through MP first. The watchdog may
         * terminate a stale process only after verifying PID start and command.
         * Stale: no status for eight seconds, or a status admitting a controller
         * silent for ten (the owner itself stops at eight). */
        long long pid = 0; double updated = 0, age = 0;
        const char *row_token, *row_start;
        status = read_json(status_path);
        if (!status) { snprintf(err, errsz, "owner starting: no status yet"); ffn_json_free(intent); return -1; }
        ffn_json_get_number(status, "updated_monotonic", &updated); ffn_json_get_number(status, "control_age_seconds", &age);
        ffn_json_get_int(status, "pid", &pid);
        row_token = ffn_json_get_string(status, "token"); row_start = ffn_json_get_string(status, "process_start");
        if ((double)now_us() / 1e6 - updated < 8 && age < 10) { ffn_json_free(status); ffn_json_free(intent); return 0; }
        if (!row_token || strcmp(row_token, intent_token) || pid <= 0 || process_start((pid_t)pid, start, sizeof start) ||
            !row_start || strcmp(row_start, start) || !owner_cmdline((pid_t)pid)) {
            snprintf(err, errsz, "Stale owner process identity changed"); ffn_json_free(status); ffn_json_free(intent); return -1;
        }
        /* The hosting daemon's one loop stalls for seconds while it attaches a
         * port, and its status and its reading of the controller stall with it;
         * killing it drops every port (three times on 2026-10-21). It is left
         * alone until its status has not moved for a minute: a hang, not a
         * busy stretch. A fresh lease adopts the group once the loop returns. */
        if (serve_cmdline((pid_t)pid) && (double)now_us() / 1e6 - updated < DAEMON_STALE) { ffn_json_free(status); ffn_json_free(intent); return 0; }
        logmsg("%s: retiring owner %lld (status %.1fs old, controller silent %.1fs)", group, pid, (double)now_us() / 1e6 - updated, age);
        kill((pid_t)pid, SIGTERM); usleep(500000);
        lock = try_lock(lock_path);
        if (lock < 0) {
            if (process_start((pid_t)pid, start, sizeof start) || strcmp(row_start, start)) {
                snprintf(err, errsz, "Owner PID changed"); ffn_json_free(status); ffn_json_free(intent); return -1;
            }
            kill((pid_t)pid, SIGKILL); usleep(200000);
            lock = try_lock(lock_path);
            if (lock < 0) { snprintf(err, errsz, "owner lock still held after SIGKILL"); ffn_json_free(status); ffn_json_free(intent); return -1; }
        }
        ffn_json_free(status);
    }
    /* Re-read under the lock: the owner may have finished its own cleanup meanwhile. */
    again = read_json(intent_path);
    if (!again && errno == ENOENT) { close(lock); ffn_json_free(intent); return 0; }
    if (!again || !ffn_json_get_string(again, "token") || strcmp(ffn_json_get_string(again, "token"), intent_token)) {
        snprintf(err, errsz, "Recovery token changed"); goto out;
    }
    {
        char *text; size_t len;
        if (read_file(dhcp_path, &text, &len) == 0) {
            pid_t dhcp = (pid_t)atoi(text);
            char *args; size_t args_len;
            free(text);
            if (dhcp > 0 && cmdline_read(dhcp, &args, &args_len) == 0) {
                bool ours = args_have(args, args_len, "udhcpc", true) && args_have(args, args_len, group, false) &&
                            args_have(args, args_len, DHCP_HOOK, false);
                free(args);
                if (!ours) { snprintf(err, errsz, "DHCP process ownership changed"); goto out; }
                /* The interface is still withdrawn if an unresponsive client
                 * survives; subsequent hook calls reject the missing intent. */
                kill(dhcp, SIGTERM);
            }
        }
    }
    network = ffn_json_get(intent, "network");
    management = network ? ffn_json_get(network, "management") : NULL;
    request = ffn_json_object();
    ffn_json_set(request, "group", ffn_json_string(group)); ffn_json_set(request, "token", ffn_json_string(intent_token));
    ffn_json_set(request, "management", management ? ffn_json_copy(management) : ffn_json_object());
    reply = runtime_verb("network-teardown", request, 60 * SECOND, err, errsz);
    ffn_json_free(request);
    if (!reply) goto out;
    ffn_json_free(reply);
    unlink(status_path); unlink(intent_path);
    rc = 0;
out:
    close(lock);
    ffn_json_free(again); ffn_json_free(intent);
    return rc;
}

static int sweep_tool(void)
{
    char pattern[PATH], err[256];
    glob_t found;
    int problems = 0;
    snprintf(pattern, sizeof pattern, "%s/ffn-aggregate-ae*-intent.json", RUN);
    if (glob(pattern, 0, NULL, &found)) return 0;
    for (size_t i = 0; i < found.gl_pathc; i++) {
        struct ffn_json *intent = read_json(found.gl_pathv[i]);
        const char *group = intent ? ffn_json_get_string(intent, "group") : NULL;
        if (group && strlen(group) <= 4 && !strncmp(group, "ae", 2) &&
            recover_group(group, ffn_json_get_string(intent, "token"), err, sizeof err) && strncmp(err, "owner starting", 14)) {
            logmsg("sweep %s: %s", group, err); problems++;
        }
        ffn_json_free(intent);
    }
    globfree(&found);
    return problems ? 1 : 0;
}

static int recover_tool(void)
{
    struct text t = { 0 };
    char buf[4096], perr[64], err[256], current_boot[40];
    ssize_t n;
    struct ffn_json *doc;
    const char *group, *token, *boot;
    while ((n = read(0, buf, sizeof buf)) > 0) text_put(&t, buf, (size_t)n);
    doc = ffn_json_parse(t.s ? t.s : "", t.len, perr, sizeof perr);
    free(t.s);
    group = doc ? ffn_json_get_string(doc, "group") : NULL; token = doc ? ffn_json_get_string(doc, "token") : NULL;
    boot = doc ? ffn_json_get_string(doc, "boot_id") : NULL;
    if (!doc || doc->count != 3 || !group || !token || !boot || boot_id(current_boot, sizeof current_boot) || strcmp(boot, current_boot) ||
        strlen(group) > 4 || strncmp(group, "ae", 2) || !uuid_text(token)) {
        printf("{\"error\": \"Fresh DP recovery identity required\"}\n"); ffn_json_free(doc); return 1;
    }
    if (recover_group(group, token, err, sizeof err) && strncmp(err, "owner starting", 14)) {
        printf("{\"error\": \"%s\"}\n", err); ffn_json_free(doc); return 1;
    }
    printf("{\"recovered\": true}\n");
    ffn_json_free(doc);
    return 0;
}

/* ---------------------------------------------------------- the daemon */
/* `ffn-portd serve`: one process owning ports 1..24 from boot. The MP pushes
 * the table of what every port should be (management/PORT-TABLE.md); the
 * daemon reconciles it against what it holds, every tick, until they agree,
 * and keeps the last accepted table in /run so a restart inside the same
 * boot re-attaches every port on its own. Aggregate leases, per-port attach
 * and detach, the faceplate observation and the status all arrive on one
 * control socket; the command lines below are its clients. */

struct server {
    int listen_fd;
    struct client clients[CLIENTS];
    struct port ports[PORTS];
    struct aggregate groups[GROUPS];
    long long revision;
    char generation[72];
    char boot[40], process_start[32];
    uint64_t next_publish;
};

static struct port *server_port(struct server *s, long long number)
{
    return number >= 1 && number <= PORTS ? &s->ports[number - 1] : NULL;
}

/* ---- the table on disk: {boot_id, revision, generation, ports: {"N": {mode, group?}}} */

static int table_save(struct server *s)
{
    struct ffn_json *doc = ffn_json_object(), *ports = ffn_json_object();
    char path[PATH];
    int rc;
    for (unsigned i = 0; i < PORTS; i++) {
        struct ffn_json *row = ffn_json_object();
        char key[8];
        snprintf(key, sizeof key, "%u", i + 1);
        ffn_json_set(row, "mode", ffn_json_string(port_mode_name(s->ports[i].desired)));
        if (s->ports[i].desired == MODE_AGGREGATE) ffn_json_set(row, "group", ffn_json_string(s->ports[i].group));
        ffn_json_set(ports, key, row);
    }
    ffn_json_set(doc, "boot_id", ffn_json_string(s->boot));
    ffn_json_set(doc, "revision", ffn_json_int(s->revision));
    ffn_json_set(doc, "generation", ffn_json_string(s->generation));
    ffn_json_set(doc, "ports", ports);
    snprintf(path, sizeof path, "%s/%s", RUN, TABLE_NAME);
    rc = write_json(path, doc);
    ffn_json_free(doc);
    return rc;
}

/* One row of a table request into a port's desired state; err names what is wrong. */
static int row_parse(const struct ffn_json *row, enum port_mode *mode, char *group, size_t groupsz, char *err, size_t errsz)
{
    const char *text, *name;
    if (!row || row->type != FFN_JSON_OBJECT) { snprintf(err, errsz, "row must be an object"); return -1; }
    text = ffn_json_get_string(row, "mode");
    if (!text) { snprintf(err, errsz, "mode required"); return -1; }
    group[0] = 0;
    if (!strcmp(text, "disabled")) *mode = MODE_DISABLED;
    else if (!strcmp(text, "l3")) *mode = MODE_L3;
    else if (!strcmp(text, "aggregate")) {
        unsigned n;
        name = ffn_json_get_string(row, "group");
        if (!name || strlen(name) < 3 || strlen(name) > 4 || strncmp(name, "ae", 2) || name[2] == '0' || strspn(name + 2, "0123456789") != strlen(name + 2)) {
            snprintf(err, errsz, "aggregate row needs group ae1..ae12"); return -1;
        }
        n = (unsigned)atoi(name + 2);
        if (n < 1 || n > GROUPS) { snprintf(err, errsz, "aggregate row needs group ae1..ae12"); return -1; }
        *mode = MODE_AGGREGATE;
        snprintf(group, groupsz, "%.7s", name);
    } else { snprintf(err, errsz, "mode must be disabled, l3 or aggregate"); return -1; }
    for (size_t i = 0; i < row->count; i++)
        if (strcmp(row->keys[i], "mode") && strcmp(row->keys[i], "group")) { snprintf(err, errsz, "unknown row field %s", row->keys[i]); return -1; }
    return 0;
}

static int table_load(struct server *s)
{
    char path[PATH];
    struct ffn_json *doc, *ports;
    const char *boot, *generation;
    long long revision = 0;
    unsigned rows = 0;
    snprintf(path, sizeof path, "%s/%s", RUN, TABLE_NAME);
    doc = read_json(path);
    if (!doc) return -1;
    boot = ffn_json_get_string(doc, "boot_id");
    ports = ffn_json_get(doc, "ports");
    if (!boot || strcmp(boot, s->boot) || !ports || ports->type != FFN_JSON_OBJECT) { ffn_json_free(doc); return -1; }
    ffn_json_get_int(doc, "revision", &revision);
    generation = ffn_json_get_string(doc, "generation");
    for (size_t i = 0; i < ports->count; i++) {
        struct port *port = server_port(s, strspn(ports->keys[i], "0123456789") == strlen(ports->keys[i]) ? atoll(ports->keys[i]) : 0);
        enum port_mode mode;
        char group[8], err[96];
        if (!port || row_parse(ports->items[i], &mode, group, sizeof group, err, sizeof err)) continue;
        port->desired = mode;
        snprintf(port->group, sizeof port->group, "%s", group);
        rows++;
    }
    s->revision = revision;
    snprintf(s->generation, sizeof s->generation, "%s", generation ? generation : "");
    ffn_json_free(doc);
    logmsg("table restored: revision %lld, %u rows", revision, rows);
    return 0;
}

/* A full table from the MP. The rows that would take a port from a live
 * aggregate lease are refused and the rest replace the table. */
static struct ffn_json *table_apply(struct server *s, const struct ffn_json *req)
{
    struct ffn_json *reply = ffn_json_object(), *rejected = ffn_json_object(), *ports;
    const char *boot = ffn_json_get_string(req, "boot_id"), *generation = ffn_json_get_string(req, "generation");
    enum port_mode wanted[PORTS];
    char groups[PORTS][8], err[96];
    ports = ffn_json_get(req, "ports");
    if (!boot || strcmp(boot, s->boot)) { ffn_json_set(reply, "error", ffn_json_string("Current DP boot identity required")); ffn_json_free(rejected); return reply; }
    if (!ports || ports->type != FFN_JSON_OBJECT) { ffn_json_set(reply, "error", ffn_json_string("ports object required")); ffn_json_free(rejected); return reply; }
    if (generation && strlen(generation) > 64) { ffn_json_set(reply, "error", ffn_json_string("generation too long")); ffn_json_free(rejected); return reply; }
    for (unsigned i = 0; i < PORTS; i++) { wanted[i] = MODE_DISABLED; groups[i][0] = 0; }
    for (size_t i = 0; i < ports->count; i++) {
        long long number = strspn(ports->keys[i], "0123456789") == strlen(ports->keys[i]) ? atoll(ports->keys[i]) : 0;
        if (!server_port(s, number)) { snprintf(err, sizeof err, "port %s: not a faceplate port", ports->keys[i]); ffn_json_set(reply, "error", ffn_json_string(err)); ffn_json_free(rejected); return reply; }
        if (row_parse(ports->items[i], &wanted[number - 1], groups[number - 1], 8, err, sizeof err)) {
            char full[128]; snprintf(full, sizeof full, "port %lld: %s", number, err);
            ffn_json_set(reply, "error", ffn_json_string(full)); ffn_json_free(rejected); return reply;
        }
    }
    for (unsigned i = 0; i < PORTS; i++) {
        struct port *port = &s->ports[i];
        char key[8];
        snprintf(key, sizeof key, "%u", i + 1);
        if (port->member_of && (wanted[i] != MODE_AGGREGATE || strcmp(groups[i], port->member_of->d.in.group))) {
            snprintf(err, sizeof err, "member of live aggregate %s; deactivate it first", port->member_of->d.in.group);
            ffn_json_set(rejected, key, ffn_json_string(err));
            continue;
        }
        if (port->desired != wanted[i] || strcmp(port->group, groups[i])) { port->next_try = 0; port->failures = 0; }
        port->desired = wanted[i];
        snprintf(port->group, sizeof port->group, "%.7s", groups[i]);
    }
    s->revision++;
    snprintf(s->generation, sizeof s->generation, "%s", generation ? generation : "");
    if (table_save(s)) logmsg("table not saved: %s", strerror(errno));
    ffn_json_set(reply, "ok", ffn_json_bool(true));
    ffn_json_set(reply, "revision", ffn_json_int(s->revision));
    ffn_json_set(reply, "rejected", rejected);
    return reply;
}

/* ---- the status: /run/ffn-portd.json and the `status` reply */

static struct ffn_json *server_status(struct server *s, uint64_t now)
{
    struct ffn_json *doc = ffn_json_object(), *ports = ffn_json_object(), *groups = ffn_json_object();
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ffn_json_set(doc, "owner", ffn_json_string("ffn-portd"));
    ffn_json_set(doc, "pid", ffn_json_int(getpid()));
    ffn_json_set(doc, "process_start", ffn_json_string(s->process_start));
    ffn_json_set(doc, "boot_id", ffn_json_string(s->boot));
    ffn_json_set(doc, "revision", ffn_json_int(s->revision));
    ffn_json_set(doc, "generation", ffn_json_string(s->generation));
    ffn_json_set(doc, "updated_monotonic", ffn_json_number((double)now / 1e6));
    ffn_json_set(doc, "updated_at", ffn_json_number((double)ts.tv_sec + (double)ts.tv_nsec / 1e9));
    for (unsigned i = 0; i < PORTS; i++) {
        struct port *port = &s->ports[i];
        struct ffn_json *row = ffn_json_object();
        char key[8];
        snprintf(key, sizeof key, "%u", i + 1);
        ffn_json_set(row, "desired", ffn_json_string(port_mode_name(port->desired)));
        ffn_json_set(row, "group", port->desired == MODE_AGGREGATE ? ffn_json_string(port->group) : ffn_json_null());
        ffn_json_set(row, "attached", ffn_json_bool(port->attached));
        ffn_json_set(row, "member_of", port->member_of ? ffn_json_string(port->member_of->d.in.group) : ffn_json_null());
        ffn_json_set(row, "reason", ffn_json_string(port->reason[0] ? port->reason : port_mode_name(port->desired)));
        ffn_json_set(row, "failures", ffn_json_int(port->failures));
        if (port->attached) {
            struct ffn_json *carrier = ffn_json_object();
            ffn_json_set(row, "owner", ffn_json_string(port->p.owner));
            ffn_json_set(row, "status", ffn_json_string(port->p.state_path));
            ffn_json_set(row, "ifindex", ffn_json_int(port->p.ifindex));
            ffn_json_set(row, "source", ffn_json_int(port->p.source));
            ffn_json_set(row, "lanes", ffn_json_int(port->d.lanes));
            ffn_json_set(row, "lanes_wanted", ffn_json_int(port->d.lanes_wanted));
            ffn_json_set(row, "multi_queue", ffn_json_bool(ffn_packet_multiqueue(port->d.native)));
            ffn_json_set(carrier, "policy", ffn_json_string(port->p.observed ? "observed" : "always"));
            ffn_json_set(carrier, "up", ffn_json_bool(port->p.observed ? port->p.carrier_up : true));
            ffn_json_set(row, "carrier", carrier);
        }
        ffn_json_set(ports, key, row);
    }
    for (unsigned i = 0; i < GROUPS; i++) {
        struct aggregate *a = &s->groups[i];
        struct ffn_json *row, *members, *distributing;
        uint16_t live[MEMBERS];
        unsigned n;
        if (a->stage == AGG_FREE || a->stage == AGG_STOPPED) continue;
        row = ffn_json_object(); members = ffn_json_array(); distributing = ffn_json_array();
        ffn_json_set(row, "stage", ffn_json_string(aggregate_stage_name(a->stage)));
        ffn_json_set(row, "token", ffn_json_string(a->d.in.token));
        for (unsigned m = 0; m < a->d.in.count; m++) ffn_json_push(members, ffn_json_int(a->d.in.members[m]));
        ffn_json_set(row, "members", members);
        n = a->d.engine ? ffn_lacp_distributing(a->d.engine, live, MEMBERS) : 0;
        for (unsigned m = 0; m < n; m++) ffn_json_push(distributing, ffn_json_int(live[m]));
        ffn_json_set(row, "distributing", distributing);
        ffn_json_set(row, "status", ffn_json_string(a->d.status_path));
        ffn_json_set(row, "lease_age_seconds", ffn_json_number(now > a->d.last_input ? (double)(now - a->d.last_input) / 1e6 : 0.0));
        ffn_json_set(row, "fault", a->d.failed ? ffn_json_string(a->d.fatal) : ffn_json_null());
        ffn_json_set(groups, a->d.in.group, row);
    }
    ffn_json_set(doc, "ports", ports);
    ffn_json_set(doc, "aggregates", groups);
    return doc;
}

static void publish(struct server *s, uint64_t now)
{
    struct ffn_json *doc;
    char path[PATH];
    if (now < s->next_publish) return;
    s->next_publish = now + PUBLISH_PERIOD;
    doc = server_status(s, now);
    snprintf(path, sizeof path, "%s/%s", RUN, DAEMON_STATUS);
    write_json(path, doc);
    ffn_json_free(doc);
}

/* ---- reconcile: the table against what is held, one attach per tick */

static void port_reason(struct port *port, const char *reason) { snprintf(port->reason, sizeof port->reason, "%.159s", reason); }

static void reconcile(struct server *s, uint64_t now)
{
    bool attached_one = false;
    for (unsigned i = 0; i < PORTS; i++) {
        struct port *port = &s->ports[i];
        char err[256];
        if (port->attached && port->d.failed) {
            snprintf(err, sizeof err, "owner fault, re-attaching: %.200s", port->d.fatal);
            port_detach(port); port_reason(port, err); port->failures++; port->next_try = now + RETRY_ATTACH;
        }
        if (port->attached && port->desired != MODE_L3) { port_detach(port); port_reason(port, port_mode_name(port->desired)); }
        if (port->desired == MODE_L3) {
            if (port->attached) continue;
            if (port->member_of) { snprintf(err, sizeof err, "held by live aggregate %s", port->member_of->d.in.group); port_reason(port, err); continue; }
            if (attached_one || now < port->next_try) continue;
            attached_one = true;
            if (port_attach(port, err, sizeof err) == 0) { port_reason(port, "running"); port->failures = 0; }
            else {
                char full[256];
                snprintf(full, sizeof full, "attach failed: %.200s", err);
                port_reason(port, full); port->failures++; port->next_try = now + RETRY_ATTACH;
                if (port->failures == 1 || port->failures % 30 == 0) logmsg("p%u: %s (attempt %u)", port->number, full, port->failures);
            }
        } else if (port->desired == MODE_AGGREGATE) {
            if (port->member_of) snprintf(err, sizeof err, "member of %s", port->member_of->d.in.group);
            else snprintf(err, sizeof err, "reserved for %s; no lease", port->group);
            port_reason(port, err);
        } else port_reason(port, port->member_of ? "held by a live aggregate" : "disabled");
    }
}

/* ---- clients */

static void client_close(struct client *c)
{
    if (c->fd >= 0) close(c->fd);
    c->fd = -1;
    free(c->in.s); free(c->out.s);
    memset(c, 0, sizeof *c);
    c->fd = -1;
}

static void client_reply(struct client *c, struct ffn_json *doc)
{
    char *text = ffn_json_dump(doc);
    if (text) { text_str(&c->out, text); text_put(&c->out, "\n", 1); }
    free(text);
    ffn_json_free(doc);
}

static struct ffn_json *error_doc(const char *message)
{
    struct ffn_json *doc = ffn_json_object();
    ffn_json_set(doc, "error", ffn_json_string(message));
    return doc;
}

/* A lease: the aggregate intent on a connection that then carries the observations. */
static void lease_begin(struct server *s, struct client *c, const struct ffn_json *req, uint64_t now)
{
    struct ffn_json *intent = ffn_json_get(req, "intent"), *members;
    struct aggregate *a = NULL;
    char *text, err[512];
    size_t len;
    if (!intent || intent->type != FFN_JSON_OBJECT) { client_reply(c, error_doc("Invalid aggregate owner intent")); c->closing = true; return; }
    for (unsigned i = 0; i < GROUPS && !a; i++) if (s->groups[i].stage == AGG_FREE) a = &s->groups[i];
    if (!a) { client_reply(c, error_doc("no free aggregate slot")); c->closing = true; return; }
    members = ffn_json_get(intent, "members");
    for (size_t i = 0; members && members->type == FFN_JSON_ARRAY && i < members->count; i++) {
        struct port *port = members->items[i]->type == FFN_JSON_INT ? server_port(s, members->items[i]->integer) : NULL;
        const char *group = ffn_json_get_string(intent, "group");
        if (!port) continue;   /* parse_intent reports an invalid member */
        if (port->attached || port->desired == MODE_L3) snprintf(err, sizeof err, "member %u is a layer-3 port; the table must make it an aggregate member first", port->number);
        else if (port->member_of) snprintf(err, sizeof err, "member %u belongs to live aggregate %s", port->number, port->member_of->d.in.group);
        else if (port->desired == MODE_AGGREGATE && group && strcmp(port->group, group)) snprintf(err, sizeof err, "member %u is reserved for %s in the table", port->number, port->group);
        else continue;
        client_reply(c, error_doc(err)); c->closing = true; return;
    }
    text = ffn_json_dump(intent);
    if (!text) { client_reply(c, error_doc("out of memory")); c->closing = true; return; }
    len = strlen(text);
    if (aggregate_begin(a, text, len, c, now, err, sizeof err)) {
        struct ffn_json *row = error_doc(err);
        ffn_json_set(row, "token", ffn_json_string(a->d.in.token)); ffn_json_set(row, "boot_id", ffn_json_string(a->d.in.boot_id));
        ffn_json_set(row, "group", ffn_json_string(a->d.in.group));
        client_reply(c, row); c->closing = true;
        logmsg("%s not started: %s", a->d.in.group[0] ? a->d.in.group : "aggregate", err);
        memset(a, 0, sizeof *a);
        free(text);
        return;
    }
    free(text);
    for (unsigned i = 0; i < a->d.in.count; i++) { struct port *port = server_port(s, a->d.in.members[i]); if (port) port->member_of = a; }
    c->agg = a;
}

static void aggregate_release(struct server *s, struct aggregate *a)
{
    aggregate_stop(a);
    for (unsigned i = 0; i < PORTS; i++) if (s->ports[i].member_of == a) s->ports[i].member_of = NULL;
    if (a->client) { a->client->agg = NULL; a->client->closing = true; }
    memset(a, 0, sizeof *a);
}

/* One request line from a connection that holds no lease. */
static void dispatch(struct server *s, struct client *c, const char *line, size_t len, uint64_t now)
{
    char perr[64], reply_text[256];
    struct ffn_json *req = ffn_json_parse(line, len, perr, sizeof perr);
    const char *op = req && req->type == FFN_JSON_OBJECT ? ffn_json_get_string(req, "op") : NULL;
    if (!op) { client_reply(c, error_doc("request must be an object with op")); ffn_json_free(req); return; }
    if (!strcmp(op, "status")) client_reply(c, server_status(s, now));
    else if (!strcmp(op, "table")) client_reply(c, table_apply(s, req));
    else if (!strcmp(op, "links")) {
        struct ffn_json *doc;
        ffn_json_remove(req, "op");
        links_record_doc(req, reply_text, sizeof reply_text);
        doc = ffn_json_parse(reply_text, strlen(reply_text), perr, sizeof perr);
        client_reply(c, doc ? doc : error_doc("cannot record the link observation"));
    } else if (!strcmp(op, "attach") || !strcmp(op, "detach")) {
        long long number = 0;
        const char *boot = ffn_json_get_string(req, "boot_id");
        struct port *port = ffn_json_get_int(req, "port", &number) ? server_port(s, number) : NULL;
        char err[256];
        if (!boot || strcmp(boot, s->boot)) client_reply(c, error_doc("Current DP boot identity required"));
        else if (!port) client_reply(c, error_doc("port 1..24 required"));
        else if (!strcmp(op, "attach")) {
            if (port->member_of) { snprintf(err, sizeof err, "port %u belongs to live aggregate %s", port->number, port->member_of->d.in.group); client_reply(c, error_doc(err)); }
            else {
                if (port->desired != MODE_L3) { port->desired = MODE_L3; port->group[0] = 0; s->revision++; table_save(s); }
                if (!port->attached) {
                    if (port_attach(port, err, sizeof err) == 0) { port_reason(port, "running"); port->failures = 0; port_tick(port, now_us()); }
                    else { char full[256]; snprintf(full, sizeof full, "attach failed: %.200s", err); port_reason(port, full); port->failures++; port->next_try = now + RETRY_ATTACH; }
                }
                if (port->attached) client_reply(c, physical_status(&port->p));
                else client_reply(c, error_doc(port->reason));
            }
        } else {
            struct physical probe;
            if (port->desired != MODE_DISABLED && port->desired != MODE_AGGREGATE) { port->desired = MODE_DISABLED; port->group[0] = 0; s->revision++; table_save(s); }
            if (port->attached) { port_detach(port); port_reason(port, "disabled"); }
            physical_select(&probe, port->number);
            client_reply(c, physical_status(&probe));
            ffn_json_free(probe.observation);
        }
    } else if (!strcmp(op, "lease")) lease_begin(s, c, req, now);
    else client_reply(c, error_doc("unknown op"));
    ffn_json_free(req);
}

static void client_read(struct server *s, struct client *c, uint64_t now)
{
    char buf[65536];
    ssize_t n = read(c->fd, buf, sizeof buf);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
        c->eof = true;
        if (c->agg) { c->agg->d.stdin_closed = true; fail(&c->agg->d, "MP control pipe closed"); }
        else c->closing = true;
        return;
    }
    if (n < 0) return;
    text_put(&c->in, buf, (size_t)n);
    if (c->agg) return;   /* the aggregate's tick consumes its lines */
    for (;;) {
        char *nl = c->in.s ? memchr(c->in.s, '\n', c->in.len) : NULL;
        size_t len;
        if (!nl) { if (c->in.len > 1 << 20) { client_reply(c, error_doc("request too long")); c->closing = true; } break; }
        len = (size_t)(nl - c->in.s);
        if (len) dispatch(s, c, c->in.s, len, now);
        memmove(c->in.s, nl + 1, c->in.len - len - 1);
        c->in.len -= len + 1; c->in.s[c->in.len] = 0;
        if (c->agg || c->closing || c->fd < 0) break;
    }
}

static void client_flush(struct client *c)
{
    while (c->fd >= 0 && c->out.len) {
        ssize_t w = write(c->fd, c->out.s, c->out.len);
        if (w < 0 && errno == EINTR) continue;
        if (w < 0 && errno == EAGAIN) break;
        if (w <= 0) { c->closing = true; c->out.len = 0; break; }
        memmove(c->out.s, c->out.s + w, c->out.len - (size_t)w);
        c->out.len -= (size_t)w; c->out.s[c->out.len] = 0;
    }
    if (c->out.len > 4 << 20) { c->closing = true; c->out.len = 0; }   /* a reader that stopped reading */
}

static int server_listen(struct server *s, char *err, size_t errsz)
{
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (snprintf(addr.sun_path, sizeof addr.sun_path, "%s/%s", RUN, SOCKET_NAME) >= (int)sizeof addr.sun_path) { snprintf(err, errsz, "socket path too long"); return -1; }
    unlink(addr.sun_path);
    s->listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (s->listen_fd < 0 || bind(s->listen_fd, (struct sockaddr *)&addr, sizeof addr) || chmod(addr.sun_path, 0600) || listen(s->listen_fd, 16)) {
        snprintf(err, errsz, "control socket %s: %s", addr.sun_path, strerror(errno)); return -1;
    }
    return 0;
}

/* What a previous instance left in /run: aggregate owners without a process
 * are torn down (the watchdog's own rule), stale port documents removed. */
static void recover_leftovers(struct server *s)
{
    char pattern[PATH], err[256];
    glob_t found;
    snprintf(pattern, sizeof pattern, "%s/ffn-aggregate-ae*-intent.json", RUN);
    if (glob(pattern, 0, NULL, &found) == 0) {
        for (size_t i = 0; i < found.gl_pathc; i++) {
            struct ffn_json *intent = read_json(found.gl_pathv[i]);
            const char *group = intent ? ffn_json_get_string(intent, "group") : NULL;
            if (group && strlen(group) <= 4 && !strncmp(group, "ae", 2)) {
                if (recover_group(group, ffn_json_get_string(intent, "token"), err, sizeof err)) logmsg("leftover %s: %s", group, err);
                else logmsg("leftover %s recovered", group);
            }
            ffn_json_free(intent);
        }
        globfree(&found);
    }
    for (unsigned i = 0; i < PORTS; i++) {
        struct physical probe;
        struct ffn_json *doc;
        long long pid = 0;
        physical_select(&probe, i + 1);
        doc = read_json(probe.state_path);
        if (doc && ffn_json_get_int(doc, "pid", &pid) && pid != (long long)getpid()) { unlink(probe.state_path); logmsg("stale %s removed (pid %lld)", probe.state_path, pid); }
        ffn_json_free(doc); ffn_json_free(probe.observation);
    }
    (void)s;
}

static int watchdog_active(void)
{
    struct helper h;
    char *const argv[] = { "/usr/bin/systemctl", "is-active", "--quiet", "ffn-aggregate-dp-watchdog.timer", NULL };
    int status = helper_run(&h, argv, NULL, 10 * SECOND);
    helper_close(&h);
    return status == 0;
}

static struct server *live_server;

static void leases_renew(void)
{
    static uint64_t next_touch;
    struct server *s = live_server;
    uint64_t now;
    if (!s) return;
    /* A path not started, paused or already faulted refuses; its own tick reports that. */
    for (unsigned i = 0; i < PORTS; i++) if (s->ports[i].d.native) (void)ffn_packet_renew(s->ports[i].d.native);
    for (unsigned i = 0; i < GROUPS; i++) if (s->groups[i].d.native) (void)ffn_packet_renew(s->groups[i].d.native);
    /* The status files are heartbeats too: the DP sweep retires a group whose
     * status is eight seconds old and the MP's owner gives up on a stale
     * observation. Once a second while the loop waits, every running group's
     * row and the daemon's own status are written again. */
    now = now_us();
    if (now < next_touch) return;
    next_touch = now + STATUS_PERIOD;
    for (unsigned i = 0; i < GROUPS; i++) {
        struct aggregate *a = &s->groups[i];
        if (a->stage == AGG_RUNNING && !a->d.failed) status_row(&a->d, now, a->current, a->ready, NULL);
    }
    publish(s, now);
}

static int serve_daemon(void)
{
    static struct server s;
    char err[512], path[PATH];
    uint64_t now;
    memset(&s, 0, sizeof s);
    live_server = &s;
    s.listen_fd = -1;
    for (unsigned i = 0; i < CLIENTS; i++) s.clients[i].fd = -1;
    for (unsigned i = 0; i < PORTS; i++) { s.ports[i].number = i + 1; s.ports[i].d.control_fd = -1; }
    signal(SIGTERM, on_stop); signal(SIGINT, on_stop); signal(SIGHUP, on_stop); signal(SIGPIPE, SIG_IGN);
    if (boot_id(s.boot, sizeof s.boot)) { logmsg("boot identity unreadable"); return 1; }
    process_start(getpid(), s.process_start, sizeof s.process_start);
    if (check_environment(err, sizeof err)) { logmsg("not started: %s", err); return 1; }
    if (!unchecked && !watchdog_active()) logmsg("warning: ffn-aggregate-dp-watchdog.timer is not active; a stalled daemon is not swept");
    recover_leftovers(&s);
    if (table_load(&s)) logmsg("no table for this boot yet: every port disabled until the MP pushes one");
    if (server_listen(&s, err, sizeof err)) { logmsg("%s", err); return 1; }
    logmsg("serving ports 1..%u (table revision %lld)", PORTS, s.revision);
    while (!stop_signal) {
        struct pollfd fds[1 + CLIENTS + 2 * GROUPS];
        unsigned n = 0, first_client, first_group;
        int ready;
        now = now_us();
        leases_renew();
        for (unsigned i = 0; i < GROUPS; i++) if (s.groups[i].stage != AGG_FREE) aggregate_tick(&s.groups[i], now);
        for (unsigned i = 0; i < GROUPS; i++) if (s.groups[i].stage != AGG_FREE && (s.groups[i].d.failed || s.groups[i].stage == AGG_STOPPED)) aggregate_release(&s, &s.groups[i]);
        reconcile(&s, now);
        for (unsigned i = 0; i < PORTS; i++) if (s.ports[i].attached) port_tick(&s.ports[i], now);
        publish(&s, now);
        for (unsigned i = 0; i < CLIENTS; i++) if (s.clients[i].fd >= 0) { client_flush(&s.clients[i]); if (s.clients[i].closing && !s.clients[i].out.len) client_close(&s.clients[i]); }
        fds[n++] = (struct pollfd){ s.listen_fd, POLLIN, 0 };
        first_client = n;
        for (unsigned i = 0; i < CLIENTS; i++) fds[n++] = (struct pollfd){ s.clients[i].fd, (short)(POLLIN | (s.clients[i].out.len ? POLLOUT : 0)), 0 };
        first_group = n;
        for (unsigned i = 0; i < GROUPS; i++) {
            struct aggregate *a = &s.groups[i];
            bool live = a->stage == AGG_RUNNING && !a->d.failed;
            fds[n++] = (struct pollfd){ live ? a->d.control_fd : -1, POLLIN, 0 };
            fds[n++] = (struct pollfd){ live && a->d.worker_running ? a->d.worker.out : -1, POLLIN, 0 };
        }
        ready = poll(fds, n, POLL_MS);
        if (ready < 0 && errno != EINTR) { logmsg("poll: %s", strerror(errno)); break; }
        now = now_us();
        if (fds[0].revents & POLLIN) {
            int fd;
            while ((fd = accept4(s.listen_fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC)) >= 0) {
                struct client *c = NULL;
                for (unsigned i = 0; i < CLIENTS && !c; i++) if (s.clients[i].fd < 0) c = &s.clients[i];
                if (!c) { close(fd); logmsg("control socket: too many clients"); continue; }
                memset(c, 0, sizeof *c); c->fd = fd;
            }
        }
        /* Drain control before packets so a withdrawal closes gates first. */
        for (unsigned i = 0; i < CLIENTS; i++) {
            struct client *c = &s.clients[i];
            if (c->fd < 0) continue;
            if (fds[first_client + i].revents & (POLLIN | POLLHUP | POLLERR)) client_read(&s, c, now);
            if (c->fd >= 0 && (fds[first_client + i].revents & POLLOUT)) client_flush(c);
        }
        for (unsigned i = 0; i < GROUPS; i++) aggregate_control_events(&s.groups[i], fds[first_group + 2 * i].revents, now);
    }
    logmsg("signal %d: stopping every owner", (int)stop_signal);
    shutting_down = true;
    alarm(SHUTDOWN_BUDGET * 2); /* a cleanup that hangs still ends: systemd restarts the daemon */
    for (unsigned i = 0; i < GROUPS; i++) if (s.groups[i].stage != AGG_FREE) { if (!s.groups[i].d.failed) fail(&s.groups[i].d, "daemon stopping"); aggregate_release(&s, &s.groups[i]); }
    for (unsigned i = 0; i < PORTS; i++) if (s.ports[i].attached) port_detach(&s.ports[i]);
    for (unsigned i = 0; i < CLIENTS; i++) if (s.clients[i].fd >= 0) { client_flush(&s.clients[i]); client_close(&s.clients[i]); }
    snprintf(path, sizeof path, "%s/%s", RUN, DAEMON_STATUS); unlink(path);
    snprintf(path, sizeof path, "%s/%s", RUN, SOCKET_NAME); unlink(path);
    if (s.listen_fd >= 0) close(s.listen_fd);
    logmsg("stopped");
    return 0;
}

/* ------------------------------------------------------- command lines */

static int read_intent_line(struct text *t)
{
    char c;
    for (;;) {
        ssize_t n = read(0, &c, 1);
        if (n <= 0) return -1;
        if (c == '\n') return 0;
        text_put(t, &c, 1);
        if (t->len > 65536) return -1;
    }
}

/* `aggregate serve`: the MP supervisor's pipe, relayed to a lease on the
 * socket. The first stdin line is the intent; every further line goes to the
 * daemon, every status row comes back, and the owner lives as long as both
 * sides do, exactly as the stdin contract read. */
static int relay_serve(void)
{
    struct text first = { 0 };
    char request_head[] = "{\"op\": \"lease\", \"intent\": ";
    char buf[65536];
    int fd, rc = 1;
    bool stdin_open = true, saw_error = false;
    if (read_intent_line(&first)) { logmsg("Invalid initial intent"); return 1; }
    fd = daemon_connect();
    if (fd < 0) { printf("{\"error\": \"ffn-portd is not running: %s\"}\n", strerror(errno)); free(first.s); return 1; }
    {
        struct text request = { 0 };
        text_str(&request, request_head); text_put(&request, first.s ? first.s : "{}", first.len ? first.len : 2); text_str(&request, "}\n");
        for (size_t off = 0; off < request.len;) {
            ssize_t w = write(fd, request.s + off, request.len - off);
            if (w < 0 && errno == EINTR) continue;
            if (w <= 0) { printf("{\"error\": \"ffn-portd rejected the lease\"}\n"); free(request.s); free(first.s); close(fd); return 1; }
            off += (size_t)w;
        }
        free(request.s);
    }
    free(first.s);
    signal(SIGPIPE, SIG_IGN);
    for (;;) {
        struct pollfd fds[2] = { { stdin_open ? 0 : -1, POLLIN, 0 }, { fd, POLLIN, 0 } };
        ssize_t n;
        if (poll(fds, 2, 1000) < 0 && errno != EINTR) break;
        if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            n = read(0, buf, sizeof buf);
            if (n <= 0) { stdin_open = false; shutdown(fd, SHUT_WR); }
            else for (ssize_t off = 0; off < n;) {
                ssize_t w = write(fd, buf + off, (size_t)(n - off));
                if (w < 0 && errno == EINTR) continue;
                if (w <= 0) { n = -1; break; }
                off += w;
            }
            if (n < 0) break;
        }
        if (fds[1].revents & (POLLIN | POLLHUP | POLLERR)) {
            n = read(fd, buf, sizeof buf);
            if (n == 0) { rc = saw_error ? 1 : 0; break; }
            if (n < 0) { if (errno == EINTR || errno == EAGAIN) continue; break; }
            if (!strncmp(buf, "{\"error\"", 8) || memmem(buf, (size_t)n, "\n{\"error\"", 9)) saw_error = true;
            for (ssize_t off = 0; off < n;) {
                ssize_t w = write(1, buf + off, (size_t)(n - off));
                if (w < 0 && errno == EINTR) continue;
                if (w <= 0) { n = -1; break; }
                off += w;
            }
            if (n < 0) break;
        }
    }
    close(fd);
    return rc;
}

/* One request to the daemon, its reply printed; 1 when the daemon is away or answered an error. */
static int daemon_cli(struct ffn_json *req, int timeout_ms, bool print)
{
    struct text reply = { 0 };
    char *text = ffn_json_dump(req);
    int fd, rc = 1;
    if (!text) return 1;
    fd = daemon_connect();
    if (fd < 0) { if (print) printf("{\"error\": \"ffn-portd is not running: %s\"}\n", strerror(errno)); free(text); return 1; }
    if (daemon_request(fd, text, strlen(text), &reply, timeout_ms) == 0) {
        char *nl = memchr(reply.s, '\n', reply.len);
        if (nl) *nl = 0;
        if (print) puts(reply.s);
        rc = reply_is_error(reply.s);
    } else if (print) printf("{\"error\": \"ffn-portd did not answer: %s\"}\n", strerror(errno));
    close(fd); free(text); free(reply.s);
    return rc;
}

/* `physical start|stop --port N`, {"boot_id": ...} on stdin: the DP-side half
 * of the MP's attach/detach, answered by the daemon's table. */
static int physical_cli(const char *verb, unsigned port)
{
    struct text t = { 0 };
    char buf[4096], perr[64], current_boot[40];
    ssize_t n;
    struct ffn_json *doc, *req;
    const char *boot;
    int rc;
    while ((n = read(0, buf, sizeof buf)) > 0) text_put(&t, buf, (size_t)n);
    doc = ffn_json_parse(t.s ? t.s : "{}", t.len ? t.len : 2, perr, sizeof perr);
    free(t.s);
    boot = doc ? ffn_json_get_string(doc, "boot_id") : NULL;
    if (!doc || doc->type != FFN_JSON_OBJECT || doc->count != 1 || !boot || boot_id(current_boot, sizeof current_boot) || strcmp(boot, current_boot)) {
        printf("{\"error\": \"Current DP boot identity required\"}\n"); ffn_json_free(doc); return 1;
    }
    ffn_json_free(doc);
    req = ffn_json_object();
    ffn_json_set(req, "op", ffn_json_string(!strcmp(verb, "start") ? "attach" : "detach"));
    ffn_json_set(req, "port", ffn_json_int(port));
    ffn_json_set(req, "boot_id", ffn_json_string(current_boot));
    rc = daemon_cli(req, 30000, true);
    ffn_json_free(req);
    return rc;
}

/* `table apply` (the table on stdin) and `table status`. */
static int table_cli(const char *verb)
{
    struct ffn_json *req;
    int rc;
    if (!strcmp(verb, "status")) {
        int fd = daemon_connect();
        if (fd < 0) {
            /* The last published status, marked as such, when the daemon is away. */
            char path[PATH], *text;
            struct ffn_json *doc;
            snprintf(path, sizeof path, "%s/%s", RUN, DAEMON_STATUS);
            doc = read_json(path);
            if (!doc) doc = ffn_json_object();
            ffn_json_set(doc, "daemon", ffn_json_bool(false));
            text = ffn_json_dump(doc);
            if (text) { puts(text); free(text); }
            ffn_json_free(doc);
            return 1;
        }
        close(fd);
        req = ffn_json_object();
        ffn_json_set(req, "op", ffn_json_string("status"));
        rc = daemon_cli(req, 10000, true);
        ffn_json_free(req);
        return rc;
    }
    if (!strcmp(verb, "apply")) {
        struct text t = { 0 };
        char buf[65536], perr[64];
        ssize_t n;
        while ((n = read(0, buf, sizeof buf)) > 0) text_put(&t, buf, (size_t)n);
        req = ffn_json_parse(t.s ? t.s : "", t.len, perr, sizeof perr);
        free(t.s);
        if (!req || req->type != FFN_JSON_OBJECT) { printf("{\"error\": \"table must be a JSON object\"}\n"); ffn_json_free(req); return 1; }
        ffn_json_set(req, "op", ffn_json_string("table"));
        rc = daemon_cli(req, 30000, true);
        ffn_json_free(req);
        return rc;
    }
    return 2;
}

/* ---------------------------------------------------------------- main */

static int revision_tool(void)
{
    struct text t = { 0 };
    char buf[4096], perr[64], out[65];
    ssize_t n;
    struct ffn_json *doc, *network;
    bool lldp = false;
    while ((n = read(0, buf, sizeof buf)) > 0) text_put(&t, buf, (size_t)n);
    doc = ffn_json_parse(t.s ? t.s : "", t.len, perr, sizeof perr);
    free(t.s);
    if (!doc || doc->type != FFN_JSON_OBJECT) { fprintf(stderr, "invalid intent: %s\n", perr); return 1; }
    network = ffn_json_get(doc, "network");
    if (!network) { fprintf(stderr, "no network\n"); ffn_json_free(doc); return 1; }
    ffn_json_get_bool(doc, "lldp", &lldp);
    network_revision(network, lldp, out);
    printf("%s\n", out);
    ffn_json_free(doc);
    return 0;
}

int main(int argc, char **argv)
{
    const char *v;
    if ((v = getenv("FFN_PORTD_RUN"))) RUN = v;
    if ((v = getenv("FFN_PORTD_NS"))) NS = v;
    if ((v = getenv("FFN_PORTD_TRUNK"))) TRUNK = v;
    if ((v = getenv("FFN_PORTD_PYTHON"))) PYTHON = v;
    if ((v = getenv("FFN_PORTD_RUNTIME"))) RUNTIME = v;
    if ((v = getenv("FFN_PORTD_DHCP_HOOK"))) DHCP_HOOK = v;
    if ((v = getenv("FFN_PORTD_STEERING"))) STEERING = v;
    if ((v = getenv("FFN_PORTD_CPUS"))) CPUS = v;
    if ((v = getenv("FFN_PORTD_CPU_RESERVATIONS"))) CPU_RESERVATIONS = v;
    if ((v = getenv("FFN_PORTD_INSPECTION"))) INSPECTION_POLICY = v;
    if ((v = getenv("FFN_PORTD_INLINE"))) INLINE_LIBRARY = v;
    if ((v = getenv("FFN_PORTD_FE100_RETURN"))) FE100_RETURN = v;
    if ((v = getenv("FFN_PORTD_DAEMON_STALE")) && atof(v) >= 1.0) DAEMON_STALE = atof(v);
    if ((v = getenv("FFN_PORTD_LANES"))) LANES_POLICY = v;
    if ((v = getenv("FFN_PORTD_NETWORK"))) NETWORK_JOURNAL = v;
    if ((v = getenv("FFN_PORTD_NETWORK_OWNER"))) NETWORK_OWNER = v;
    if ((v = getenv("FFN_PORTD_ROUTE_LINKS"))) ROUTE_LINKS = v;
    unchecked = (v = getenv("FFN_PORTD_UNCHECKED")) && !strcmp(v, "1");
    if ((v = getenv("FFN_PORTD_COPPER_PROFILE"))) COPPER_PROFILE = v;
    if ((v = getenv("FFN_PORTD_CARRIER_POLICY"))) CARRIER_POLICY = v;
    if (argc == 2 && !strcmp(argv[1], "serve")) return serve_daemon();
    if (argc == 3 && !strcmp(argv[1], "aggregate")) {
        if (!strcmp(argv[2], "serve")) return relay_serve();
        if (!strcmp(argv[2], "sweep")) return sweep_tool();
        if (!strcmp(argv[2], "recover")) return recover_tool();
        if (!strcmp(argv[2], "revision")) return revision_tool();
    }
    if (argc == 3 && !strcmp(argv[1], "table")) return table_cli(argv[2]);
    if (argc == 5 && !strcmp(argv[1], "physical") && !strcmp(argv[3], "--port")) {
        struct physical p;
        unsigned port = (unsigned)atoi(argv[4]);
        if (physical_select(&p, port)) { fprintf(stderr, "ffn-portd: port 1..24 required\n"); return 2; }
        ffn_json_free(p.observation);
        if (!strcmp(argv[2], "status")) {
            struct ffn_json *status = physical_status(&p);
            char *text = ffn_json_dump(status);
            if (text) { puts(text); free(text); }
            ffn_json_free(status);
            return 0;
        }
        if (!strcmp(argv[2], "start") || !strcmp(argv[2], "stop")) return physical_cli(argv[2], port);
    }
    if (argc == 2 && !strcmp(argv[1], "links")) return links_tool();
    fprintf(stderr, "usage: ffn-portd serve                      (the daemon: ports 1..24 from boot, the table, the leases)\n"
                    "       ffn-portd table apply|status          (the MP's port table on stdin / the table status)\n"
                    "       ffn-portd aggregate serve|sweep|recover|revision\n"
                    "       ffn-portd physical start|stop|status --port N\n"
                    "       ffn-portd links                       (the MP's faceplate observation on stdin)\n");
    return 2;
}
