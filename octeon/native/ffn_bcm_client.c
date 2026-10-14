/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_bcm_client.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static void seterr(char *err, size_t errsz, const char *message) { if (err && errsz) snprintf(err, errsz, "%s", message); }

static int connect_bounded(const char *host, int port, int seconds)
{
    struct addrinfo hints = { 0 }, *list = NULL;
    char service[8];
    hints.ai_family = AF_INET; hints.ai_socktype = SOCK_STREAM;
    snprintf(service, sizeof service, "%d", port);
    if (getaddrinfo(host, service, &hints, &list)) return -1;
    int fd = -1;
    for (struct addrinfo *a = list; a; a = a->ai_next) {
        fd = socket(a->ai_family, a->ai_socktype | SOCK_NONBLOCK, a->ai_protocol);
        if (fd < 0) continue;
        if (connect(fd, a->ai_addr, a->ai_addrlen) == 0) break;
        if (errno == EINPROGRESS) {
            struct pollfd p = { fd, POLLOUT, 0 };
            int soerr = 0; socklen_t len = sizeof soerr;
            if (poll(&p, 1, seconds * 1000) == 1 && getsockopt(fd, SOL_SOCKET, SO_ERROR, &soerr, &len) == 0 && soerr == 0) break;
        }
        close(fd); fd = -1;
    }
    freeaddrinfo(list);
    if (fd >= 0) {
        int flags = fcntl(fd, F_GETFL, 0);
        fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
        struct timeval tv = { 70, 0 };
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv); setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    }
    return fd;
}

struct ffn_json *ffn_bcm_call(const char *host, int port, const struct ffn_json *request, char *err, size_t errsz)
{
    char *line = ffn_json_dump(request);
    if (!line) { seterr(err, errsz, "out of memory"); return NULL; }
    size_t len = strlen(line);
    char *wire = malloc(len + 2);
    if (!wire) { free(line); seterr(err, errsz, "out of memory"); return NULL; }
    memcpy(wire, line, len); wire[len] = '\n'; wire[len + 1] = 0; free(line);
    int fd = connect_bounded(host, port, 5);
    if (fd < 0) { free(wire); seterr(err, errsz, "BCM service unreachable"); return NULL; }
    for (size_t off = 0; off < len + 1;) {
        ssize_t sent = write(fd, wire + off, len + 1 - off);
        if (sent <= 0) { free(wire); close(fd); seterr(err, errsz, "BCM service write failed"); return NULL; }
        off += (size_t)sent;
    }
    free(wire);
    size_t cap = 65536, used = 0;
    char *buf = malloc(cap);
    struct ffn_json *reply = NULL;
    while (buf) {
        if (used + 1 >= cap) {
            if (cap >= 1024 * 1024 + 2) { seterr(err, errsz, "BCM response too large"); break; }
            char *n = realloc(buf, cap * 2); if (!n) break; buf = n; cap *= 2;
        }
        ssize_t got = read(fd, buf + used, cap - used - 1);
        if (got <= 0) { seterr(err, errsz, "BCM service read failed"); break; }
        used += (size_t)got;
        char *nl = memchr(buf, '\n', used);
        if (nl) {
            char perr[96];
            reply = ffn_json_parse(buf, (size_t)(nl - buf), perr, sizeof perr);
            if (!reply) seterr(err, errsz, "BCM response is not valid JSON");
            break;
        }
    }
    free(buf); close(fd);
    if (!reply) return NULL;
    bool ok = false;
    if (!ffn_json_get_bool(reply, "ok", &ok) || !ok) { ffn_json_free(reply); seterr(err, errsz, "BCM operation failed; refresh hardware status"); return NULL; }
    return reply;
}

int ffn_lock_wait(const char *path, int seconds)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    struct timespec deadline; clock_gettime(CLOCK_MONOTONIC, &deadline); deadline.tv_sec += seconds;
    for (;;) {
        if (flock(fd, LOCK_EX | LOCK_NB) == 0) return fd;
        if (errno != EWOULDBLOCK && errno != EINTR) break;
        struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec > deadline.tv_sec || (now.tv_sec == deadline.tv_sec && now.tv_nsec >= deadline.tv_nsec)) break;
        struct timespec nap = { 0, 20 * 1000 * 1000 };
        nanosleep(&nap, NULL);
    }
    close(fd);
    return -1;
}

static int read_whole(const char *path, char **data, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    size_t cap = 4096, used = 0; char *buf = malloc(cap);
    while (buf) {
        size_t got = fread(buf + used, 1, cap - used, f); used += got;
        if (got == 0) break;
        if (used == cap) { char *n = realloc(buf, cap * 2); if (!n) { free(buf); buf = NULL; break; } buf = n; cap *= 2; }
    }
    fclose(f);
    if (!buf) return -1;
    *data = buf; *len = used; return 0;
}

static int write_whole(const char *path, const char *data, size_t len)
{
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    int rc = fwrite(data, 1, len, f) == len ? 0 : -1;
    if (fclose(f)) rc = -1;
    return rc;
}

struct ffn_json *ffn_bcm_cint(const char *host, int port, const char *script_path, const char *lock_path, int lock_seconds,
                              const char *recipe, int timeout, char *err, size_t errsz)
{
    int lock = ffn_lock_wait(lock_path, lock_seconds);
    if (lock < 0) { seterr(err, errsz, "Aggregate hardware lock busy"); return NULL; }
    char *previous = NULL; size_t plen = 0;
    bool existed = read_whole(script_path, &previous, &plen) == 0;
    struct ffn_json *markers = NULL;
    if (write_whole(script_path, recipe, strlen(recipe))) { seterr(err, errsz, "cannot stage SDK recipe"); goto out; }
    const char *name = strrchr(script_path, '/'); name = name ? name + 1 : script_path;
    struct ffn_json *req = ffn_json_object();
    if (!req || ffn_json_set(req, "op", ffn_json_string("cint.run")) || ffn_json_set(req, "script", ffn_json_string(name)) ||
        ffn_json_set(req, "timeout", ffn_json_int(timeout))) { ffn_json_free(req); seterr(err, errsz, "out of memory"); goto restore; }
    struct ffn_json *reply = ffn_bcm_call(host, port, req, err, errsz);
    ffn_json_free(req);
    if (!reply) goto restore;
    bool completed = false, truncated = false;
    ffn_json_get_bool(reply, "completed", &completed); ffn_json_get_bool(reply, "truncated", &truncated);
    if (!completed || truncated) { ffn_json_free(reply); seterr(err, errsz, "SDK recipe incomplete; inspect BCM diagnostics"); goto restore; }
    const struct ffn_json *m = ffn_json_get(reply, "markers");
    markers = m && m->type == FFN_JSON_ARRAY ? ffn_json_copy(m) : ffn_json_array();
    ffn_json_free(reply);
restore:
    if (existed) write_whole(script_path, previous, plen);
    else unlink(script_path);
out:
    free(previous);
    close(lock);
    return markers;
}

int ffn_bcm_epoch(const char *proc_root, char *out, size_t outsz, char *err, size_t errsz)
{
    DIR *dir = opendir(proc_root);
    if (!dir) { seterr(err, errsz, "cannot read process table"); return -1; }
    char owner[320] = ""; int owners = 0;
    struct dirent *e;
    while ((e = readdir(dir))) {
        if (e->d_name[0] < '0' || e->d_name[0] > '9') continue;
        char path[640];
        if (snprintf(path, sizeof path, "%s/%s/cmdline", proc_root, e->d_name) >= (int)sizeof path) continue;
        FILE *f = fopen(path, "rb");
        if (!f) continue;
        char cmd[4096];
        size_t n = fread(cmd, 1, sizeof cmd - 1, f);
        fclose(f);
        cmd[n] = 0;
        bool match = false;
        for (size_t i = 0; i < n;) {
            const char *arg = cmd + i; size_t alen = strlen(arg);
            const char *base = strrchr(arg, '/'); base = base ? base + 1 : arg;
            if (alen && strcmp(base, "ffn_bcmd.py") == 0) match = true;
            i += alen + 1;
        }
        if (!match) continue;
        if (snprintf(path, sizeof path, "%s/%s/stat", proc_root, e->d_name) >= (int)sizeof path) continue;
        f = fopen(path, "r");
        if (!f) continue;
        char stat[1024];
        n = fread(stat, 1, sizeof stat - 1, f);
        fclose(f);
        stat[n] = 0;
        const char *paren = strrchr(stat, ')');
        if (!paren) continue;
        const char *p = paren + 2; char start[32] = "";
        for (int field = 0; field < 20 && *p; field++) {
            const char *q = p; while (*q && *q != ' ') q++;
            if (field == 19) { size_t l = (size_t)(q - p) < sizeof start - 1 ? (size_t)(q - p) : sizeof start - 1; memcpy(start, p, l); start[l] = 0; }
            p = *q ? q + 1 : q;
        }
        if (!start[0]) continue;
        if (snprintf(owner, sizeof owner, "%s:%s", e->d_name, start) >= (int)sizeof owner) continue;
        owners++;
    }
    closedir(dir);
    if (owners != 1) { seterr(err, errsz, "one BCM owner process required"); return -1; }
    char path[640], boot[80] = "";
    if (snprintf(path, sizeof path, "%s/sys/kernel/random/boot_id", proc_root) >= (int)sizeof path) { seterr(err, errsz, "proc path too long"); return -1; }
    FILE *f = fopen(path, "r");
    if (!f || !fgets(boot, sizeof boot, f)) { if (f) fclose(f); seterr(err, errsz, "cannot read boot identity"); return -1; }
    fclose(f);
    boot[strcspn(boot, "\r\n")] = 0;
    if (snprintf(out, outsz, "%s:%s", boot, owner) >= (int)outsz) { seterr(err, errsz, "epoch too long"); return -1; }
    return 0;
}
