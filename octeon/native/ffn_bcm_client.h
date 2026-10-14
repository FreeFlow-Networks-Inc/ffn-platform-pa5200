/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Client for the CP's BCM service (ffn_bcmd.py, TCP 127.1.1.2:8104, one JSON
 * line per request and reply) and the bounded cint recipe runner the owners
 * share: the recipe is staged as /usr/share/broadcom/ffn_bcm_forward_test.c
 * under /run/ffn-forward-test.lock and restored afterwards. Also the BCM
 * owner lifetime ("<boot_id>:<pid>:<starttime>" of the single ffn_bcmd.py). */
#ifndef FFN_BCM_CLIENT_H
#define FFN_BCM_CLIENT_H
#include "ffn_json.h"
#include <stddef.h>

#define FFN_BCM_HOST "127.1.1.2"
#define FFN_BCM_PORT 8104
#define FFN_BCM_SCRIPT "/usr/share/broadcom/ffn_bcm_forward_test.c"
#define FFN_BCM_SCRIPT_LOCK "/run/ffn-forward-test.lock"

/* One request/reply; NULL with err on transport failure or a reply without "ok": true. */
struct ffn_json *ffn_bcm_call(const char *host, int port, const struct ffn_json *request, char *err, size_t errsz);
/* Run a cint recipe through the service; returns the markers array (caller frees) or NULL with err.
 * The recipe file is swapped under the lock for up to lock_seconds and restored afterwards. */
struct ffn_json *ffn_bcm_cint(const char *host, int port, const char *script_path, const char *lock_path, int lock_seconds,
                              const char *recipe, int timeout, char *err, size_t errsz);
/* Exclusive flock on path within seconds; fd or -1. */
int ffn_lock_wait(const char *path, int seconds);
/* The BCM owner lifetime from proc_root (normally /proc). 0 or -1 with err. */
int ffn_bcm_epoch(const char *proc_root, char *out, size_t outsz, char *err, size_t errsz);

#endif
