/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ffn-portctl: the CP owner of one independent physical port's packet
 * redirect (front BCM port -> TM_SSP trunk 24 -> OCTEON dataplane).
 *
 * Same contract as ffn_physical_forwarding.py: actions status, start and stop
 * with a JSON payload on stdin and a JSON observation on stdout, journaled in
 * /etc/ffn/physical-<port>.json, fenced by the BCM owner lifetime (epoch) and
 * serialised by the per-port, faceplate and fabric locks. Every hardware
 * change is a bounded SDK recipe through the BCM service with a readback.
 * Locks are waited for (seconds, not a single second): ffn-copper-link holds
 * the faceplate lock four seconds of every six, which is where the Python
 * attach lost half its calls.
 *
 * All I/O goes through struct portctl_io so the control flow is testable
 * against a fake BCM service, fake lifetime and a temporary state directory. */
#ifndef FFN_PORTCTL_H
#define FFN_PORTCTL_H
#include "ffn_json.h"
#include <stddef.h>

#define PORTCTL_TRUNK 24
#define PORTCTL_QUEUES 8
#define PORTCTL_HEADER_TM_SSP 11
#define PORTCTL_LOCK_SECONDS 15
#define PORTCTL_CINT_TIMEOUT 30

struct portctl_paths {
    const char *etc;      /* /etc/ffn */
    const char *run;      /* /run */
    const char *script;   /* /usr/share/broadcom/ffn_bcm_forward_test.c */
};

struct portctl_io {
    void *ctx;
    /* BCM service request/response (one JSON line each). NULL with err on transport failure. */
    struct ffn_json *(*bcm_call)(void *ctx, const struct ffn_json *request, char *err, size_t errsz);
    /* BCM owner lifetime: "<boot_id>:<pid>:<starttime>". 0 on success, -1 with err. */
    int (*epoch)(void *ctx, char *out, size_t outsz, char *err, size_t errsz);
    /* Exclusive lock on path within seconds; fd >= 0, or -1 with err. */
    int (*lock)(void *ctx, const char *path, int seconds, char *err, size_t errsz);
    void (*unlock)(void *ctx, int fd);
    struct ffn_json *(*read_json)(void *ctx, const char *path);           /* NULL when absent or invalid */
    int (*write_json)(void *ctx, const char *path, const struct ffn_json *value, char *err, size_t errsz);  /* atomic, fsynced */
    int (*read_file)(void *ctx, const char *path, char **data, size_t *len);   /* -1 when absent */
    int (*write_file)(void *ctx, const char *path, const char *data, size_t len);
    int (*unlink)(void *ctx, const char *path);
};

/* Front faceplate port (1..24) -> BCM port, 0 when unmapped. Port 1 is WAN1 (own owner). */
int portctl_bcm_port(int front);

/* Parse the markers of a WAN-style redirect readback. 0 on success, -1 with err. */
int portctl_parse_redirect(const struct ffn_json *markers, struct ffn_json **hardware, char *err, size_t errsz);
/* Parse the markers of a fabric readback for bcm_port. 0 on success, -1 with err. */
int portctl_parse_fabric(const struct ffn_json *markers, int bcm_port, struct ffn_json **row, char *err, size_t errsz);

/* Canonical lowercase RFC 4122 text form. */
int portctl_canonical_uuid(const char *text);

/* Run one action. On success *out is a malloc'd JSON line and 0 is returned; otherwise -1 with err
 * (2 for a rejected request, 1 for a runtime failure, in *status). */
int portctl_execute(const char *action, const char *payload, size_t length, const struct portctl_paths *paths,
                    const struct portctl_io *io, char **out, int *status, char *err, size_t errsz);

/* The production I/O: TCP BCM service at host:port, flock, /proc lifetime scan, atomic files. */
const struct portctl_io *portctl_system_io(const char *bcm_host, int bcm_port, const char *proc_root);

#endif
