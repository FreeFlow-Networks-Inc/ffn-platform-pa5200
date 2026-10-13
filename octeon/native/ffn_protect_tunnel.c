/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn-protect-tunnel: the tunnel device of one FFN Protect gateway.
 *
 * Owns the TUN device, terminates ESP-in-UDP (RFC 4303 in UDP 4501, the
 * GlobalProtect keying handed out by the gateway's getconfig) for every ESP
 * session, and moves packets for SSL-tunnel sessions to and from the Python
 * control plane over socket pairs. The control plane drives it over a
 * SOCK_SEQPACKET unix socket with one JSON object per message:
 *
 *   {"op":"add","id":7,"client":"10.200.0.5","spi_in":"0x12345678",
 *    "spi_out":"0x9abcdef0","enc":"<hex>","auth":"<hex>"}     -> {"ok":true}
 *   {"op":"ssl","id":8,"client":"10.200.0.6"} + one fd (SCM_RIGHTS) -> {"ok":true}
 *   {"op":"del","id":7}                                       -> {"ok":true}
 *   {"op":"stats"}                     -> {"ok":true,"sessions":[...],"tun":{...}}
 *
 * spi_in is the SPI the client sends (gateway receives), spi_out the one the
 * gateway sends. Keys are hex: enc 16 or 32 bytes (aes-128-cbc / aes-256-cbc),
 * auth 20 bytes (HMAC-SHA1, 96-bit ICV). Inbound packets must come from the
 * session's client address inside the tunnel or they are dropped; the
 * client's public address and port are learned from each authenticated
 * packet, so NAT traversal needs nothing else.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/if_tun.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>

#define MAX_SESSIONS 1024
#define MAX_CONTROL 8
#define MAX_PACKET 2048
#define ESP_HDR 8
#define ESP_IV 16
#define ESP_ICV 12
#define REPLAY_WINDOW 64
#define SESSION_ESP 1
#define SESSION_SSL 2

struct session {
    int used, mode, id;
    uint32_t client;            /* host order */
    uint32_t spi_in, spi_out;   /* host order */
    unsigned char enc[32], auth[20];
    int enc_len;
    uint32_t seq_out, seq_high; /* seq_high: highest seq seen inbound */
    uint64_t replay;            /* bitmap below seq_high */
    struct sockaddr_in peer;    /* learned */
    int ssl_fd;
    uint64_t rx_packets, tx_packets, rx_bytes, tx_bytes, drops, replays, auth_failures;
    time_t last_rx;
};

static struct session sessions[MAX_SESSIONS];
static int tun_fd = -1, udp_fd = -1;
static int clients[MAX_CONTROL];
static int client_count;
static uint64_t tun_rx, tun_tx, tun_unknown, udp_rx, udp_unknown_spi, udp_keepalives, udp_malformed;
static volatile sig_atomic_t stopping;

static void on_signal(int sig) { (void)sig; stopping = 1; }

static struct session *by_spi(uint32_t spi) {
    for (int i = 0; i < MAX_SESSIONS; i++)
        if (sessions[i].used && sessions[i].mode == SESSION_ESP && sessions[i].spi_in == spi) return &sessions[i];
    return NULL;
}
static struct session *by_client(uint32_t client) {
    for (int i = 0; i < MAX_SESSIONS; i++)
        if (sessions[i].used && sessions[i].client == client) return &sessions[i];
    return NULL;
}
static struct session *by_id(int id) {
    for (int i = 0; i < MAX_SESSIONS; i++)
        if (sessions[i].used && sessions[i].id == id) return &sessions[i];
    return NULL;
}
static struct session *free_slot(void) {
    for (int i = 0; i < MAX_SESSIONS; i++)
        if (!sessions[i].used) { memset(&sessions[i], 0, sizeof(sessions[i])); sessions[i].ssl_fd = -1; return &sessions[i]; }
    return NULL;
}

static int open_tun(const char *name) {
    struct ifreq ifr;
    int fd = open("/dev/net/tun", O_RDWR | O_CLOEXEC);
    if (fd < 0) return -1;
    memset(&ifr, 0, sizeof(ifr));
    ifr.ifr_flags = IFF_TUN | IFF_NO_PI;
    snprintf(ifr.ifr_name, IFNAMSIZ, "%s", name);
    if (ioctl(fd, TUNSETIFF, &ifr) < 0) { int e = errno; close(fd); errno = e; return -1; }
    return fd;
}

static int open_udp(const char *bind_address, int port) {
    struct sockaddr_in addr;
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0), yes = 1, size = 4 * 1024 * 1024;
    if (fd < 0) return -1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &size, sizeof(size));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = bind_address ? inet_addr(bind_address) : INADDR_ANY;
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) { int e = errno; close(fd); errno = e; return -1; }
    return fd;
}

static int open_control(const char *path) {
    struct sockaddr_un addr;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
    unlink(path);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(fd, 4) < 0) { int e = errno; close(fd); errno = e; return -1; }
    chmod(path, 0600);
    return fd;
}

/* ---- ESP ------------------------------------------------------------- */

static int hmac_sha1_96(const unsigned char *key, const unsigned char *data, size_t len, unsigned char *out12) {
    unsigned char mac[EVP_MAX_MD_SIZE];
    unsigned int mac_len = 0;
    if (!HMAC(EVP_sha1(), key, 20, data, len, mac, &mac_len) || mac_len < ESP_ICV) return -1;
    memcpy(out12, mac, ESP_ICV);
    return 0;
}

static const EVP_CIPHER *cipher_for(int enc_len) { return enc_len == 32 ? EVP_aes_256_cbc() : EVP_aes_128_cbc(); }

static int replay_check(struct session *s, uint32_t seq) {
    if (seq == 0) return -1;
    if (seq > s->seq_high) return 0;
    uint32_t diff = s->seq_high - seq;
    if (diff >= REPLAY_WINDOW) return -1;
    return (s->replay & ((uint64_t)1 << diff)) ? -1 : 0;
}
static void replay_update(struct session *s, uint32_t seq) {
    if (seq > s->seq_high) {
        uint32_t shift = seq - s->seq_high;
        s->replay = shift >= REPLAY_WINDOW ? 0 : s->replay << shift;
        s->replay |= 1;
        s->seq_high = seq;
    } else {
        s->replay |= (uint64_t)1 << (s->seq_high - seq);
    }
}

/* Decrypt one ESP datagram in place; returns the inner packet length or -1. */
int esp_unwrap(struct session *s, unsigned char *buf, int len, unsigned char *out, int out_cap) {
    unsigned char icv[ESP_ICV];
    int body, plain = 0, tail = 0;
    if (len < ESP_HDR + ESP_IV + ESP_ICV + 16) return -1;
    uint32_t spi = ntohl(*(uint32_t *)buf), seq = ntohl(*(uint32_t *)(buf + 4));
    if (spi != s->spi_in) return -1;
    if (replay_check(s, seq)) { s->replays++; return -1; }
    body = len - ESP_ICV;
    if (hmac_sha1_96(s->auth, buf, (size_t)body, icv) || CRYPTO_memcmp(icv, buf + body, ESP_ICV)) { s->auth_failures++; return -1; }
    int ct_len = body - ESP_HDR - ESP_IV;
    if (ct_len <= 0 || ct_len % 16 || ct_len > out_cap) return -1;
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return -1;
    if (!EVP_DecryptInit_ex(ctx, cipher_for(s->enc_len), NULL, s->enc, buf + ESP_HDR) ||
        !EVP_CIPHER_CTX_set_padding(ctx, 0) ||
        !EVP_DecryptUpdate(ctx, out, &plain, buf + ESP_HDR + ESP_IV, ct_len) ||
        !EVP_DecryptFinal_ex(ctx, out + plain, &tail)) { EVP_CIPHER_CTX_free(ctx); return -1; }
    EVP_CIPHER_CTX_free(ctx);
    plain += tail;
    if (plain < 2) return -1;
    int pad_len = out[plain - 2], next = out[plain - 1];
    if (next != 4 || pad_len + 2 > plain) return -1;   /* IPv4 only in this phase */
    replay_update(s, seq);
    return plain - 2 - pad_len;
}

/* Wrap one IPv4 packet into an ESP datagram; returns the datagram length or -1. */
int esp_wrap(struct session *s, const unsigned char *packet, int len, unsigned char *out, int out_cap) {
    int pad_len = (16 - (len + 2) % 16) % 16, plain_len = len + pad_len + 2, enc_len = 0, tail = 0;
    unsigned char plain[MAX_PACKET + 32];
    if (len <= 0 || plain_len > (int)sizeof(plain) || ESP_HDR + ESP_IV + plain_len + ESP_ICV > out_cap) return -1;
    memcpy(plain, packet, (size_t)len);
    for (int i = 0; i < pad_len; i++) plain[len + i] = (unsigned char)(i + 1);
    plain[len + pad_len] = (unsigned char)pad_len;
    plain[len + pad_len + 1] = 4;
    *(uint32_t *)out = htonl(s->spi_out);
    *(uint32_t *)(out + 4) = htonl(++s->seq_out);
    if (RAND_bytes(out + ESP_HDR, ESP_IV) != 1) return -1;
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    if (!ctx) return -1;
    if (!EVP_EncryptInit_ex(ctx, cipher_for(s->enc_len), NULL, s->enc, out + ESP_HDR) ||
        !EVP_CIPHER_CTX_set_padding(ctx, 0) ||
        !EVP_EncryptUpdate(ctx, out + ESP_HDR + ESP_IV, &enc_len, plain, plain_len) ||
        !EVP_EncryptFinal_ex(ctx, out + ESP_HDR + ESP_IV + enc_len, &tail)) { EVP_CIPHER_CTX_free(ctx); return -1; }
    EVP_CIPHER_CTX_free(ctx);
    enc_len += tail;
    int body = ESP_HDR + ESP_IV + enc_len;
    if (hmac_sha1_96(s->auth, out, (size_t)body, out + body)) return -1;
    return body + ESP_ICV;
}

/* ---- forwarding -------------------------------------------------------- */

static uint32_t ipv4_src(const unsigned char *p) { return ntohl(*(const uint32_t *)(p + 12)); }
static uint32_t ipv4_dst(const unsigned char *p) { return ntohl(*(const uint32_t *)(p + 16)); }
static int ipv4_ok(const unsigned char *p, int len) { return len >= 20 && (p[0] >> 4) == 4 && ntohs(*(const uint16_t *)(p + 2)) <= len; }

static void deliver_to_tun(struct session *s, const unsigned char *packet, int len) {
    if (!ipv4_ok(packet, len) || ipv4_src(packet) != s->client) { s->drops++; return; }
    if (write(tun_fd, packet, (size_t)len) == len) { s->rx_packets++; s->rx_bytes += (uint64_t)len; s->last_rx = time(NULL); tun_tx++; }
    else s->drops++;
}

static void handle_udp(void) {
    unsigned char buf[MAX_PACKET + 64], plain[MAX_PACKET + 32];
    struct sockaddr_in from;
    socklen_t from_len = sizeof(from);
    ssize_t n = recvfrom(udp_fd, buf, sizeof(buf), MSG_DONTWAIT, (struct sockaddr *)&from, &from_len);
    if (n < 0) return;
    udp_rx++;
    if (n == 1 && buf[0] == 0xff) { udp_keepalives++; return; }
    if (n < ESP_HDR) { udp_malformed++; return; }
    struct session *s = by_spi(ntohl(*(uint32_t *)buf));
    if (!s) { udp_unknown_spi++; return; }
    int len = esp_unwrap(s, buf, (int)n, plain, sizeof(plain));
    if (len < 0) { s->drops++; return; }
    s->peer = from;   /* authenticated: the client's current public endpoint */
    deliver_to_tun(s, plain, len);
}

static void handle_tun(void) {
    unsigned char packet[MAX_PACKET], out[MAX_PACKET + 64];
    ssize_t n = read(tun_fd, packet, sizeof(packet));
    if (n <= 0) return;
    tun_rx++;
    if (!ipv4_ok(packet, (int)n)) { tun_unknown++; return; }
    struct session *s = by_client(ipv4_dst(packet));
    if (!s) { tun_unknown++; return; }
    if (s->mode == SESSION_SSL) {
        if (s->ssl_fd >= 0 && send(s->ssl_fd, packet, (size_t)n, MSG_DONTWAIT | MSG_NOSIGNAL) == n) { s->tx_packets++; s->tx_bytes += (uint64_t)n; }
        else s->drops++;
        return;
    }
    if (!s->peer.sin_port) { s->drops++; return; }   /* nothing authenticated from this client yet */
    int len = esp_wrap(s, packet, (int)n, out, sizeof(out));
    if (len < 0 || sendto(udp_fd, out, (size_t)len, MSG_DONTWAIT, (struct sockaddr *)&s->peer, sizeof(s->peer)) != len) { s->drops++; return; }
    s->tx_packets++; s->tx_bytes += (uint64_t)n;
}

static void handle_ssl_session(struct session *s) {
    unsigned char packet[MAX_PACKET];
    ssize_t n = recv(s->ssl_fd, packet, sizeof(packet), MSG_DONTWAIT);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(s->ssl_fd); s->ssl_fd = -1; s->used = 0; return; }
    if (n > 0) deliver_to_tun(s, packet, (int)n);
}

/* ---- control ----------------------------------------------------------- */

static const char *json_string(const char *json, const char *key, char *out, size_t cap) {
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\":", key);
    const char *p = strstr(json, pattern);
    if (!p) return NULL;
    p += strlen(pattern);
    while (*p == ' ') p++;
    if (*p == '"') {
        p++;
        size_t i = 0;
        while (*p && *p != '"' && i + 1 < cap) out[i++] = *p++;
        out[i] = 0;
        return *p == '"' ? out : NULL;
    }
    size_t i = 0;
    while ((*p >= '0' && *p <= '9') && i + 1 < cap) out[i++] = *p++;
    out[i] = 0;
    return i ? out : NULL;
}

static int hex_decode(const char *hex, unsigned char *out, int cap) {
    int n = 0;
    for (; hex[0] && hex[1]; hex += 2) {
        unsigned v;
        if (n >= cap || sscanf(hex, "%2x", &v) != 1) return -1;
        out[n++] = (unsigned char)v;
    }
    return hex[0] ? -1 : n;
}

static void reply(int fd, const char *text) { send(fd, text, strlen(text), MSG_NOSIGNAL); }

static void stats_reply(int fd) {
    char *out = malloc(65536);
    if (!out) { reply(fd, "{\"ok\":false,\"error\":\"memory\"}"); return; }
    int n = snprintf(out, 65536, "{\"ok\":true,\"tun\":{\"rx\":%llu,\"tx\":%llu,\"unknown\":%llu},"
                     "\"udp\":{\"rx\":%llu,\"unknown_spi\":%llu,\"keepalives\":%llu,\"malformed\":%llu},\"sessions\":[",
                     (unsigned long long)tun_rx, (unsigned long long)tun_tx, (unsigned long long)tun_unknown,
                     (unsigned long long)udp_rx, (unsigned long long)udp_unknown_spi, (unsigned long long)udp_keepalives,
                     (unsigned long long)udp_malformed);
    int first = 1;
    for (int i = 0; i < MAX_SESSIONS && n < 60000; i++) {
        struct session *s = &sessions[i];
        if (!s->used) continue;
        char peer[32] = "";
        if (s->peer.sin_port) snprintf(peer, sizeof(peer), "%s:%u", inet_ntoa(s->peer.sin_addr), ntohs(s->peer.sin_port));
        struct in_addr c = { .s_addr = htonl(s->client) };
        n += snprintf(out + n, 65536 - (size_t)n, "%s{\"id\":%d,\"mode\":\"%s\",\"client\":\"%s\",\"peer\":\"%s\",\"rx_packets\":%llu,\"tx_packets\":%llu,"
                      "\"rx_bytes\":%llu,\"tx_bytes\":%llu,\"drops\":%llu,\"replays\":%llu,\"auth_failures\":%llu,\"last_rx\":%lld}",
                      first ? "" : ",", s->id, s->mode == SESSION_ESP ? "esp" : "ssl", inet_ntoa(c), peer,
                      (unsigned long long)s->rx_packets, (unsigned long long)s->tx_packets, (unsigned long long)s->rx_bytes,
                      (unsigned long long)s->tx_bytes, (unsigned long long)s->drops, (unsigned long long)s->replays,
                      (unsigned long long)s->auth_failures, (long long)s->last_rx);
        first = 0;
    }
    snprintf(out + n, 65536 - (size_t)n, "]}");
    reply(fd, out);
    free(out);
}

static void handle_control(int fd) {
    char buf[4096], value[128];
    struct iovec iov = { buf, sizeof(buf) - 1 };
    char cmsgbuf[CMSG_SPACE(sizeof(int))];
    struct msghdr msg = { .msg_iov = &iov, .msg_iovlen = 1, .msg_control = cmsgbuf, .msg_controllen = sizeof(cmsgbuf) };
    int passed_fd = -1;
    ssize_t n = recvmsg(fd, &msg, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (n <= 0) {
        for (int i = 0; i < client_count; i++) if (clients[i] == fd) { clients[i] = clients[--client_count]; break; }
        close(fd);
        return;
    }
    buf[n] = 0;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c))
        if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) memcpy(&passed_fd, CMSG_DATA(c), sizeof(int));
    if (!json_string(buf, "op", value, sizeof(value))) { if (passed_fd >= 0) close(passed_fd); reply(fd, "{\"ok\":false,\"error\":\"op\"}"); return; }
    if (!strcmp(value, "stats")) { if (passed_fd >= 0) close(passed_fd); stats_reply(fd); return; }
    if (!strcmp(value, "ping")) { if (passed_fd >= 0) close(passed_fd); reply(fd, "{\"ok\":true}"); return; }
    char idtext[32];
    if (!json_string(buf, "id", idtext, sizeof(idtext))) { if (passed_fd >= 0) close(passed_fd); reply(fd, "{\"ok\":false,\"error\":\"id\"}"); return; }
    int id = atoi(idtext);
    if (!strcmp(value, "del")) {
        struct session *s = by_id(id);
        if (s) { if (s->ssl_fd >= 0) close(s->ssl_fd); s->used = 0; }
        if (passed_fd >= 0) close(passed_fd);
        reply(fd, s ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"unknown session\"}");
        return;
    }
    if (strcmp(value, "add") && strcmp(value, "ssl")) { if (passed_fd >= 0) close(passed_fd); reply(fd, "{\"ok\":false,\"error\":\"unknown op\"}"); return; }
    char client[64];
    struct in_addr addr;
    if (!json_string(buf, "client", client, sizeof(client)) || !inet_aton(client, &addr) || by_id(id) || by_client(ntohl(addr.s_addr))) {
        if (passed_fd >= 0) close(passed_fd);
        reply(fd, "{\"ok\":false,\"error\":\"client or id invalid or in use\"}");
        return;
    }
    struct session *s = free_slot();
    if (!s) { if (passed_fd >= 0) close(passed_fd); reply(fd, "{\"ok\":false,\"error\":\"session table full\"}"); return; }
    s->id = id; s->client = ntohl(addr.s_addr);
    if (!strcmp(value, "ssl")) {
        if (passed_fd < 0) { reply(fd, "{\"ok\":false,\"error\":\"ssl session needs a descriptor\"}"); return; }
        s->mode = SESSION_SSL; s->ssl_fd = passed_fd; s->used = 1;
        reply(fd, "{\"ok\":true}");
        return;
    }
    if (passed_fd >= 0) close(passed_fd);
    char spi_in[32], spi_out[32], enc[128], auth[128];
    if (!json_string(buf, "spi_in", spi_in, sizeof(spi_in)) || !json_string(buf, "spi_out", spi_out, sizeof(spi_out)) ||
        !json_string(buf, "enc", enc, sizeof(enc)) || !json_string(buf, "auth", auth, sizeof(auth))) {
        reply(fd, "{\"ok\":false,\"error\":\"esp parameters\"}"); return;
    }
    s->spi_in = (uint32_t)strtoul(spi_in, NULL, 0); s->spi_out = (uint32_t)strtoul(spi_out, NULL, 0);
    s->enc_len = hex_decode(enc, s->enc, sizeof(s->enc));
    if ((s->enc_len != 16 && s->enc_len != 32) || hex_decode(auth, s->auth, sizeof(s->auth)) != 20 || !s->spi_in || !s->spi_out || by_spi(s->spi_in)) {
        memset(s, 0, sizeof(*s)); reply(fd, "{\"ok\":false,\"error\":\"esp keys or spi\"}"); return;
    }
    s->mode = SESSION_ESP; s->ssl_fd = -1; s->used = 1;
    reply(fd, "{\"ok\":true}");
}

#ifndef FFN_PROTECT_TUNNEL_TEST
int main(int argc, char **argv) {
    const char *tun = NULL, *control = NULL, *bind_address = NULL;
    int port = 4501;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--tun") && i + 1 < argc) tun = argv[++i];
        else if (!strcmp(argv[i], "--control") && i + 1 < argc) control = argv[++i];
        else if (!strcmp(argv[i], "--bind") && i + 1 < argc) bind_address = argv[++i];
        else if (!strcmp(argv[i], "--port") && i + 1 < argc) port = atoi(argv[++i]);
        else { fprintf(stderr, "usage: %s --tun NAME --control PATH [--bind ADDR] [--port 4501]\n", argv[0]); return 2; }
    }
    if (!tun || !control) { fprintf(stderr, "--tun and --control are required\n"); return 2; }
    signal(SIGTERM, on_signal); signal(SIGINT, on_signal); signal(SIGPIPE, SIG_IGN);
    for (int i = 0; i < MAX_SESSIONS; i++) sessions[i].ssl_fd = -1;
    if ((tun_fd = open_tun(tun)) < 0) { perror("tun"); return 1; }
    if ((udp_fd = open_udp(bind_address, port)) < 0) { perror("udp"); return 1; }
    int control_fd = open_control(control);
    if (control_fd < 0) { perror("control"); return 1; }
    fprintf(stderr, "ffn-protect-tunnel: %s ready, udp %s:%d, control %s\n", tun, bind_address ? bind_address : "*", port, control);
    while (!stopping) {
        struct pollfd fds[3 + MAX_CONTROL + MAX_SESSIONS];
        struct session *ssl_sessions[MAX_SESSIONS];
        int n = 0, ssl_count = 0;
        fds[n++] = (struct pollfd){ tun_fd, POLLIN, 0 };
        fds[n++] = (struct pollfd){ udp_fd, POLLIN, 0 };
        fds[n++] = (struct pollfd){ control_fd, POLLIN, 0 };
        for (int i = 0; i < client_count; i++) fds[n++] = (struct pollfd){ clients[i], POLLIN, 0 };
        for (int i = 0; i < MAX_SESSIONS; i++)
            if (sessions[i].used && sessions[i].mode == SESSION_SSL && sessions[i].ssl_fd >= 0) { ssl_sessions[ssl_count++] = &sessions[i]; fds[n++] = (struct pollfd){ sessions[i].ssl_fd, POLLIN, 0 }; }
        int ready = poll(fds, (nfds_t)n, 1000);
        if (ready <= 0) continue;
        int k = 0;
        if (fds[k++].revents & POLLIN) for (int b = 0; b < 64; b++) { handle_tun(); }
        if (fds[k++].revents & POLLIN) for (int b = 0; b < 64; b++) { handle_udp(); }
        if (fds[k++].revents & POLLIN) {
            int fd = accept4(control_fd, NULL, NULL, SOCK_CLOEXEC);
            if (fd >= 0) { if (client_count < MAX_CONTROL) clients[client_count++] = fd; else close(fd); }
        }
        int snapshot = client_count;
        for (int i = 0; i < snapshot; i++, k++) if (fds[k].revents & (POLLIN | POLLHUP | POLLERR)) handle_control(fds[k].fd);
        for (int i = 0; i < ssl_count; i++, k++) if (fds[k].revents & (POLLIN | POLLHUP | POLLERR)) handle_ssl_session(ssl_sessions[i]);
    }
    fprintf(stderr, "ffn-protect-tunnel: stopping\n");
    return 0;
}
#endif
