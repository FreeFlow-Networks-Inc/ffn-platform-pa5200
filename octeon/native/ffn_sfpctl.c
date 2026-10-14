/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ffn-sfpctl: the CP's SFP+ cage control binary (ethernet1/5 .. 1/20).
 *
 *   ffn-sfpctl inventory                 cage presence and transmitter state, {"5": {...}, ...}
 *   ffn-sfpctl set-tx <port> on|off      drive TX_DISABLE for one cage; the cage state after readback
 *   ffn-sfpctl identity <port>           the present module's validated identity summary (+ a0 page hex)
 *   ffn-sfpctl diagnostics <port>        decoded SFF-8472 diagnostics
 *   ffn-sfpctl link <port> <speed>       apply the module's link mode for auto|1000|10000; {"mode": ...}
 *   ffn-sfpctl relink <port>             re-apply the configured speed to the module present now
 *   ffn-sfpctl check [--ports 5,8] [--no-diagnostics]
 *                                        the transmit check; publishes /run/ffn-sfp-health.json
 *   ffn-sfpctl watch                     the insertion watcher (foreground; events as JSON lines)
 *
 * Replies are one JSON document on stdout. Failures are {"error": "..."} on
 * stdout with exit status 1; usage errors exit 2. Environment: FFN_SFP_ROOT
 * prefixes every path (tests), FFN_BCM_HOST/FFN_BCM_PORT select the BCM
 * service, FFN_SFP_SYSFS the I2C sysfs tree. */
#define _GNU_SOURCE
#include "ffn_bcm_client.h"
#include "ffn_sfp.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* libffn-hwio (ffn_hwio.c), linked in: bounded I2C transfers through /dev/i2c-N. */
int ffn_i2c_read(unsigned bus, unsigned address, unsigned offset, unsigned width, uint8_t *out, unsigned count);
int ffn_i2c_write8(unsigned bus, unsigned address, unsigned offset, unsigned value);

static const char *bcm_host = FFN_BCM_HOST;
static int bcm_port = FFN_BCM_PORT;
static const char *sysfs = "/sys/bus/i2c/devices";
static char root[PATH_MAX] = "";

static const char *rooted(const char *path)
{
    static char slots[8][PATH_MAX]; static unsigned next;
    char *out = slots[next++ % 8];
    size_t rl = strlen(root), pl = strlen(path);
    if (rl + pl >= PATH_MAX) { out[0] = 0; return out; }
    memcpy(out, root, rl); memcpy(out + rl, path, pl + 1);
    return out;
}

/* ---- the real world ---- */

static int io_i2c_read(void *ctx, unsigned bus, unsigned addr, unsigned offset, unsigned width, uint8_t *out, unsigned count)
{ (void)ctx; return ffn_i2c_read(bus, addr, offset, width, out, count); }
static int io_i2c_write8(void *ctx, unsigned bus, unsigned addr, unsigned offset, unsigned value)
{ (void)ctx; return ffn_i2c_write8(bus, addr, offset, value); }

static int io_module_bus(void *ctx, int port, char *err, size_t errsz)
{
    (void)ctx;
    int bit = sfp_bit(port);
    if (bit < 0) { snprintf(err, errsz, "SFP control requires a faceplate port from 5 through 20"); return -1; }
    char mux[PATH_MAX], channel[PATH_MAX], target[PATH_MAX];
    if (snprintf(mux, sizeof mux, "%s/%s", sysfs, bit < 8 ? "1-0075" : "1-0077") >= (int)sizeof mux ||
        snprintf(channel, sizeof channel, "%s/channel-%d", mux, bit % 8) >= (int)sizeof channel) { snprintf(err, errsz, "sysfs path too long"); return -1; }
    ssize_t n = readlink(channel, target, sizeof target - 1);
    if (n < 0) { snprintf(err, errsz, "SFP diagnostic I2C multiplexer is not bound"); return -1; }
    target[n] = 0;
    char driver[PATH_MAX], drv[PATH_MAX];
    if (snprintf(driver, sizeof driver, "%s/driver", mux) >= (int)sizeof driver) { snprintf(err, errsz, "sysfs path too long"); return -1; }
    n = readlink(driver, drv, sizeof drv - 1);
    if (n < 0) { snprintf(err, errsz, "Unexpected SFP multiplexer driver"); return -1; }
    drv[n] = 0;
    const char *dname = strrchr(drv, '/'); dname = dname ? dname + 1 : drv;
    if (strcmp(dname, "pca954x")) { snprintf(err, errsz, "Unexpected SFP multiplexer driver"); return -1; }
    const char *base = strrchr(target, '/'); base = base ? base + 1 : target;
    const char *dash = strrchr(base, '-');
    if (!dash || !dash[1]) { snprintf(err, errsz, "SFP diagnostic I2C multiplexer is not bound"); return -1; }
    char *end; long adapter = strtol(dash + 1, &end, 10);
    if (*end || adapter < 0) { snprintf(err, errsz, "SFP diagnostic I2C multiplexer is not bound"); return -1; }
    return (int)adapter;
}

static int io_lock(void *ctx, const char *path, int seconds) { (void)ctx; return ffn_lock_wait(path, seconds); }
static void io_unlock(void *ctx, int fd) { (void)ctx; if (fd >= 0) close(fd); }
static struct ffn_json *io_bcm(void *ctx, const struct ffn_json *request, char *err, size_t errsz)
{ (void)ctx; return ffn_bcm_call(bcm_host, bcm_port, request, err, errsz); }
static struct ffn_json *io_cint(void *ctx, const char *recipe, int timeout, char *err, size_t errsz)
{ (void)ctx; return ffn_bcm_cint(bcm_host, bcm_port, rooted(FFN_BCM_SCRIPT), rooted(FFN_BCM_SCRIPT_LOCK), SFP_LOCK_SECONDS, recipe, timeout, err, errsz); }

static struct ffn_json *io_read_json(void *ctx, const char *path)
{
    (void)ctx;
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    size_t cap = 4096, used = 0; char *buf = malloc(cap);
    while (buf) {
        size_t got = fread(buf + used, 1, cap - used, f); used += got;
        if (!got) break;
        if (used == cap) { char *n = realloc(buf, cap * 2); if (!n) { free(buf); buf = NULL; break; } buf = n; cap *= 2; }
    }
    fclose(f);
    if (!buf) return NULL;
    struct ffn_json *v = ffn_json_parse(buf, used, NULL, 0);
    free(buf);
    return v;
}

static int mkdir_parent(const char *path)
{
    char dir[PATH_MAX]; snprintf(dir, sizeof dir, "%s", path);
    char *slash = strrchr(dir, '/');
    if (!slash || slash == dir) return 0;
    *slash = 0;
    for (char *p = dir + 1; *p; p++) if (*p == '/') { *p = 0; if (mkdir(dir, 0755) && errno != EEXIST) return -1; *p = '/'; }
    if (mkdir(dir, 0755) && errno != EEXIST) return -1;
    return 0;
}

static int io_write_json(void *ctx, const char *path, const struct ffn_json *value)
{
    (void)ctx;
    char *text = ffn_json_dump(value);
    if (!text) return -1;
    char tmp[PATH_MAX]; snprintf(tmp, sizeof tmp, "%s.tmp", path);
    mkdir_parent(path);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) { free(text); return -1; }
    size_t len = strlen(text); int rc = 0;
    for (size_t off = 0; off < len;) { ssize_t w = write(fd, text + off, len - off); if (w <= 0) { rc = -1; break; } off += (size_t)w; }
    if (rc == 0 && fsync(fd)) rc = -1;
    if (close(fd)) rc = -1;
    free(text);
    if (rc == 0 && rename(tmp, path)) rc = -1;
    if (rc) unlink(tmp);
    return rc;
}

static int io_unlink(void *ctx, const char *path) { (void)ctx; return unlink(path) == 0 || errno == ENOENT ? 0 : -1; }
static int io_epoch(void *ctx, char *out, size_t outsz) { (void)ctx; char err[96]; return ffn_bcm_epoch(rooted("/proc"), out, outsz, err, sizeof err); }
static time_t io_now(void *ctx) { (void)ctx; return time(NULL); }
static void io_log(void *ctx, const char *line) { (void)ctx; printf("%s\n", line); fflush(stdout); }

static const struct sfp_io REAL = {
    NULL, io_i2c_read, io_i2c_write8, io_module_bus, io_lock, io_unlock, io_bcm, io_cint,
    io_read_json, io_write_json, io_unlink, io_epoch, io_now, io_log,
};

/* ---- output ---- */

static int reply(struct ffn_json *doc)
{
    char *text = ffn_json_dump(doc);
    ffn_json_free(doc);
    if (!text) return 1;
    printf("%s\n", text); free(text);
    return 0;
}

static int fail_with(const char *message)
{
    struct ffn_json *o = ffn_json_object(); ffn_json_set(o, "error", ffn_json_string(message));
    reply(o);
    return 1;
}

static int parse_port(const char *text, int *port)
{
    char *end; long v = strtol(text, &end, 10);
    if (*text == 0 || *end || v < SFP_FIRST_PORT || v > SFP_LAST_PORT) return -1;
    *port = (int)v; return 0;
}

static void hex_page(const uint8_t *page, size_t len, char *out)
{
    for (size_t i = 0; i < len; i++) sprintf(out + 2 * i, "%02x", page[i]);
    out[2 * len] = 0;
}

static volatile sig_atomic_t stopping;
static void on_term(int sig) { (void)sig; stopping = 1; }

static int usage(void)
{
    fprintf(stderr, "usage: ffn-sfpctl inventory | set-tx <port> on|off | identity <port> | diagnostics <port> | link <port> <speed> | relink <port> | check [--ports P,P] [--no-diagnostics] | watch\n");
    return 2;
}

int main(int argc, char **argv)
{
    const char *env;
    if ((env = getenv("FFN_SFP_ROOT")) && env[0]) snprintf(root, sizeof root, "%s", env);
    if ((env = getenv("FFN_BCM_HOST")) && env[0]) bcm_host = env;
    if ((env = getenv("FFN_BCM_PORT")) && env[0]) bcm_port = atoi(env);
    if ((env = getenv("FFN_SFP_SYSFS")) && env[0]) sysfs = env;
    struct sfp_paths paths = {
        rooted("/run/ffn-sfp-control.lock"), rooted("/run/ffn-i2c-bus1.lock"), rooted("/run/ffn-faceplate.lock"),
        rooted("/etc/ffn/faceplate.json"), rooted("/etc/ffn"), rooted("/etc/ffn/aggregate-hardware.json"),
        rooted("/run/ffn-sfp-modules"), rooted("/run/ffn-sfp-health.json"),
    };
    if (argc < 2) return usage();
    const char *verb = argv[1];
    char err[200] = "";
    if (!strcmp(verb, "inventory") && argc == 2) {
        struct sfp_cage cages[SFP_PORTS];
        if (sfp_inventory(&REAL, &paths, cages, err, sizeof err)) return fail_with(err);
        struct ffn_json *o = ffn_json_object();
        for (int p = SFP_FIRST_PORT; p <= SFP_LAST_PORT; p++) { char key[8]; snprintf(key, sizeof key, "%d", p); ffn_json_set(o, key, sfp_cage_json(&cages[p - SFP_FIRST_PORT])); }
        return reply(o);
    }
    if (!strcmp(verb, "set-tx") && argc == 4) {
        int port; bool on;
        if (parse_port(argv[2], &port)) return fail_with("SFP control requires a faceplate port from 5 through 20");
        if (!strcmp(argv[3], "on")) on = true; else if (!strcmp(argv[3], "off")) on = false; else return usage();
        struct sfp_cage cage;
        if (sfp_set_enabled(&REAL, &paths, port, on, &cage, err, sizeof err)) return fail_with(err);
        return reply(sfp_cage_json(&cage));
    }
    if (!strcmp(verb, "identity") && argc == 3) {
        int port; uint8_t a0[96]; char hex[2 * 96 + 1];
        if (parse_port(argv[2], &port)) return fail_with("SFP control requires a faceplate port from 5 through 20");
        if (sfp_identity(&REAL, &paths, port, a0, err, sizeof err)) return fail_with(err);
        struct ffn_json *o = sfp_summary_json(a0);
        hex_page(a0, 96, hex); ffn_json_set(o, "a0", ffn_json_string(hex));
        return reply(o);
    }
    if (!strcmp(verb, "diagnostics") && argc == 3) {
        int port; uint8_t a0[96], a2[128]; bool has_a2 = false; struct ffn_json *o = NULL;
        if (parse_port(argv[2], &port)) return fail_with("SFP control requires a faceplate port from 5 through 20");
        if (sfp_read_diagnostics(&REAL, &paths, port, a0, a2, &has_a2, err, sizeof err)) return fail_with(err);
        if (sfp_decode_diagnostics(a0, has_a2 ? a2 : NULL, has_a2 ? 128 : 0, &o, err, sizeof err)) return fail_with(err);
        return reply(o);
    }
    if (!strcmp(verb, "link") && argc == 4) {
        int port; struct ffn_json *mode = NULL;
        if (parse_port(argv[2], &port)) return fail_with("SFP control requires a faceplate port from 5 through 20");
        int rc = sfp_configure_fiber(&REAL, &paths, port, sfp_bcm_port(port), argv[3], NULL, &mode, err, sizeof err);
        if (rc < 0) return fail_with(err);
        struct ffn_json *o = ffn_json_object();
        ffn_json_set(o, "mode", rc ? mode : ffn_json_string("generic"));
        return reply(o);
    }
    if (!strcmp(verb, "relink") && argc == 3) {
        int port;
        if (parse_port(argv[2], &port)) return fail_with("SFP control requires a faceplate port from 5 through 20");
        struct ffn_json *o = sfp_relink(&REAL, &paths, port, err, sizeof err);
        if (!o) return fail_with(err);
        return reply(o);
    }
    if (!strcmp(verb, "check")) {
        int ports[SFP_PORTS]; unsigned count = 0; bool diagnostics = true;
        for (int i = 2; i < argc; i++) {
            if (!strcmp(argv[i], "--no-diagnostics")) diagnostics = false;
            else if (!strcmp(argv[i], "--ports") && i + 1 < argc) {
                char list[256]; snprintf(list, sizeof list, "%s", argv[++i]);
                for (char *tok = strtok(list, ","); tok; tok = strtok(NULL, ",")) {
                    int port; if (parse_port(tok, &port) || count >= SFP_PORTS) return fail_with("ports must be within 5..20");
                    ports[count++] = port;
                }
            } else return usage();
        }
        if (!count) for (int p = SFP_FIRST_PORT; p <= SFP_LAST_PORT; p++) ports[count++] = p;
        struct ffn_json *results = sfp_collect(&REAL, &paths, ports, count, diagnostics, err, sizeof err);
        if (!results) return fail_with(err);
        char boot[80] = ""; FILE *f = fopen(rooted("/proc/sys/kernel/random/boot_id"), "r");
        if (f) { if (!fgets(boot, sizeof boot, f)) boot[0] = 0; fclose(f); boot[strcspn(boot, "\r\n")] = 0; }
        long long now = (long long)time(NULL);
        if (sfp_publish(&REAL, &paths, results, boot[0] ? boot : NULL, now)) { ffn_json_free(results); return fail_with("cannot publish the SFP health record"); }
        struct ffn_json *doc = ffn_json_object();
        ffn_json_set(doc, "schema", ffn_json_int(1)); ffn_json_set(doc, "boot_id", boot[0] ? ffn_json_string(boot) : ffn_json_null());
        ffn_json_set(doc, "checked_at", ffn_json_int(now)); ffn_json_set(doc, "ports", results);
        return reply(doc);
    }
    if (!strcmp(verb, "watch") && argc == 2) {
        struct sfp_watch w; sfp_watch_init(&w);
        struct sigaction sa = { 0 }; sa.sa_handler = on_term; sigaction(SIGTERM, &sa, NULL); sigaction(SIGINT, &sa, NULL);
        while (!stopping) {
            sfp_watch_poll(&w, &REAL, &paths);
            for (unsigned i = 0; i < w.interval && !stopping; i++) sleep(1);
        }
        return 0;
    }
    return usage();
}
