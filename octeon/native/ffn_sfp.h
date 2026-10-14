/* SPDX-License-Identifier: GPL-2.0-or-later */
/* PA-5200 SFP+ cage control (ethernet1/5 .. 1/20) in C: the board's PCA9555
 * presence/TX_DISABLE expanders on I2C bus 1, SFF-8472 identity and
 * diagnostics behind the PCA954x cage multiplexers, the link mode an optical
 * module needs on the BCM88375 (1000BASE-X + Clause 37 for 1 Gb/s optics, XFI
 * for 10 Gb/s), the transmit check that names the first failing layer, and
 * the insertion watcher that re-links a port when a module appears.
 *
 * Bus 1 is shared with the thermal governor: every transfer is at most eight
 * bytes (longer ones crawl at ~25 ms/byte on the OCTEON TWSI path) and runs
 * under /run/ffn-i2c-bus1.lock, which the governor holds per sample.
 *
 * All I/O is injected through struct sfp_io so the logic is testable against
 * recorded EEPROM pages and a fake BCM service. */
#ifndef FFN_SFP_H
#define FFN_SFP_H
#include "ffn_json.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#define SFP_FIRST_PORT 5
#define SFP_LAST_PORT 20
#define SFP_PORTS 16
#define SFP_BUS 1
#define SFP_PRESENCE_EXPANDER 0x22
#define SFP_CONTROL_EXPANDER 0x23
#define SFP_CHUNK 8
#define SFP_LOCK_SECONDS 15
#define SFP_LASER_MIN_DBM (-20.0)
#define SFP_RX_MIN_DBM (-30.0)

struct sfp_paths {
    const char *control_lock;     /* /run/ffn-sfp-control.lock */
    const char *bus_lock;         /* /run/ffn-i2c-bus1.lock */
    const char *faceplate_lock;   /* /run/ffn-faceplate.lock */
    const char *faceplate_state;  /* /etc/ffn/faceplate.json */
    const char *journal_dir;      /* /etc/ffn (physical-<port>.json) */
    const char *aggregate_state;  /* /etc/ffn/aggregate-hardware.json */
    const char *modules_dir;      /* /run/ffn-sfp-modules (identity cache) */
    const char *health;           /* /run/ffn-sfp-health.json */
};

struct sfp_io {
    void *ctx;
    int (*i2c_read)(void *ctx, unsigned bus, unsigned addr, unsigned offset, unsigned width, uint8_t *out, unsigned count);
    int (*i2c_write8)(void *ctx, unsigned bus, unsigned addr, unsigned offset, unsigned value);
    int (*module_bus)(void *ctx, int port, char *err, size_t errsz);          /* the cage's adapter behind the mux */
    int (*lock)(void *ctx, const char *path, int seconds);
    void (*unlock)(void *ctx, int fd);
    struct ffn_json *(*bcm)(void *ctx, const struct ffn_json *request, char *err, size_t errsz);
    struct ffn_json *(*cint)(void *ctx, const char *recipe, int timeout, char *err, size_t errsz);   /* markers */
    struct ffn_json *(*read_json)(void *ctx, const char *path);
    int (*write_json)(void *ctx, const char *path, const struct ffn_json *value);
    int (*unlink)(void *ctx, const char *path);
    int (*epoch)(void *ctx, char *out, size_t outsz);
    time_t (*now)(void *ctx);
    void (*log)(void *ctx, const char *line);
};

struct sfp_cage { bool present, tx_enabled, tx_disable, control_ready; };

/* Board map: the expander bit of a faceplate port (5..20), or -1. */
int sfp_bit(int port);
/* BCM port of a faceplate port 5..20, or 0. */
int sfp_bcm_port(int port);

/* Cage controls (PCA9555, under the control lock). */
int sfp_inventory(const struct sfp_io *, const struct sfp_paths *, struct sfp_cage out[SFP_PORTS], char *err, size_t errsz);
int sfp_set_enabled(const struct sfp_io *, const struct sfp_paths *, int port, bool enabled, struct sfp_cage *out, char *err, size_t errsz);
struct ffn_json *sfp_cage_json(const struct sfp_cage *);

/* Module pages (under the control lock and the bus lock, 8-byte transactions). */
int sfp_identity(const struct sfp_io *, const struct sfp_paths *, int port, uint8_t a0[96], char *err, size_t errsz);
int sfp_read_diagnostics(const struct sfp_io *, const struct sfp_paths *, int port, uint8_t a0[96], uint8_t a2[128], bool *has_a2, char *err, size_t errsz);

/* SFF-8472 decoding (pure). */
int sfp_validate_identity(const uint8_t a0[96]);
bool sfp_optical(const uint8_t a0[96]);
unsigned sfp_module_speeds(const uint8_t a0[96], int rates[2]);           /* ascending Mb/s; 0 when undeclared */
struct ffn_json *sfp_summary_json(const uint8_t a0[96]);                   /* vendor, part, wavelength_nm, optical, speeds */
int sfp_decode_diagnostics(const uint8_t a0[96], const uint8_t *a2, size_t a2len, struct ffn_json **out, char *err, size_t errsz);
/* 1 = optical mode resolved (*rate, *autoneg), 0 = not classifiable as optics (generic path), -1 = refused with err. */
int sfp_link_mode(const uint8_t a0[96], const char *speed, int *rate, bool *autoneg, char *err, size_t errsz);

/* Apply the module's link mode on the switch for a requested speed through the cint endpoint.
 * 1 = applied (*mode: speed, autoneg, interface, link_mode, changed, autoneg_mode_control), 0 = not optics
 * (the caller keeps the generic SDK link path), -1 = failure with err. a0 may be NULL (read here). */
int sfp_configure_fiber(const struct sfp_io *, const struct sfp_paths *, int port, int chip, const char *speed, const uint8_t *a0,
                        struct ffn_json **mode, char *err, size_t errsz);

/* The transmit check: one port's verdict from its layers (pure), and the live collection. */
struct ffn_json *sfp_check_port(int port, const struct sfp_cage *cage, const struct ffn_json *module, const struct ffn_json *diagnostics,
                                const struct ffn_json *link, const struct ffn_json *attachment, const struct ffn_json *committed);
struct ffn_json *sfp_collect(const struct sfp_io *, const struct sfp_paths *, const int *ports, unsigned count, bool diagnostics, char *err, size_t errsz);
int sfp_publish(const struct sfp_io *, const struct sfp_paths *, const struct ffn_json *results, const char *boot_id, long long checked_at);

/* Re-apply a port's configured speed to the module present now (under the faceplate lock). */
struct ffn_json *sfp_relink(const struct sfp_io *, const struct sfp_paths *, int port, char *err, size_t errsz);

/* The insertion watcher. */
struct sfp_watch {
    bool have_previous;
    bool previous[SFP_PORTS];
    time_t pending_deadline[SFP_PORTS];   /* 0 = not pending */
    time_t quiet_until, next_check;
    char verdict[SFP_PORTS][24];
    unsigned interval, settle, identity_deadline, backoff, check_interval;
};
void sfp_watch_init(struct sfp_watch *);
void sfp_watch_poll(struct sfp_watch *, const struct sfp_io *, const struct sfp_paths *);

#endif
