/* SPDX-License-Identifier: GPL-2.0-or-later
 * Commissioning bridge from Ethernet frames to the existing bounded DLP engine.
 * No packet-path allocation. No stream reassembly or decryption. Unsupported
 * protocols/fragments return -2; malformed lengths return -1. Caller decides
 * whether to pass these and must count them separately from inspected traffic.
 */
#include <stdlib.h>
#include <string.h>
#include "ffn_dp_engine.h"
#include "ffn_dp_dlp.h"

struct inline_state {
    struct dp_engine_set engines;
    struct dp_dlp dlp;
};

void *ffn_inline_create(const unsigned char *pattern, unsigned length, int action)
{
    struct inline_state *s;
    if (!pattern || !length || length >= DP_DLP_PAT_MAX ||
        (action != DP_EV_ALERT && action != DP_EV_BLOCK)) return NULL;
    s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->dlp.count = 1;
    memcpy(s->dlp.rule[0].pattern, pattern, length);
    s->dlp.rule[0].pattern_len = length;
    strcpy(s->dlp.rule[0].name, "commissioning-literal");
    s->dlp.rule[0].type = DP_DLP_KEYWORD;
    s->dlp.rule[0].action = action;
    s->dlp.rule[0].enabled = 1;
    dp_engine_register(&s->engines, "dlp", dp_dlp_scan, &s->dlp);
    dp_engine_enable(&s->engines, "dlp", 1);
    return s;
}

void ffn_inline_destroy(void *state) { free(state); }
static unsigned be16(const unsigned char *p) { return ((unsigned)p[0] << 8) | p[1]; }

int ffn_inline_scan(void *state, const unsigned char *f, unsigned length)
{
    unsigned off = 14, type, end, protocol, hlen, total;
    struct dp_engine_ctx ctx = {0};
    if (!state || !f || length < 14 || length > 65535) return -1;
    type = be16(f+12);
    for (unsigned tags = 0; type == 0x8100 || type == 0x88a8; tags++) {
        if (tags == 2) return -2;
        if (length < off+4) return -1;
        type = be16(f+off+2);
        off += 4;
    }
    if (type == 0x0800) {
        if (length < off+20 || (f[off] >> 4) != 4) return -1;
        hlen = (f[off] & 15)*4;
        total = be16(f+off+2);
        if (hlen < 20 || total < hlen || total > length-off) return -1;
        if (be16(f+off+6) & 0x3fff) return -2;
        protocol = f[off+9];
        end = off+total;
        off += hlen;
    } else if (type == 0x86dd) {
        if (length < off+40 || (f[off] >> 4) != 6) return -1;
        total = be16(f+off+4);
        if (!total) return -2; /* no jumbogram/extension-header interpretation */
        if (total > length-off-40) return -1;
        protocol = f[off+6];
        off += 40;
        end = off+total;
    } else return -2;
    if (protocol == 17) {
        if (end-off < 8) return -1;
        total = be16(f+off+4);
        if (total < 8 || total != end-off) return -1;
        hlen = 8;
    } else if (protocol == 6) {
        if (end-off < 20) return -1;
        hlen = (f[off+12] >> 4)*4;
        if (hlen < 20 || hlen > end-off) return -1;
    } else return -2;
    ctx.l4_proto = protocol;
    ctx.dport = be16(f+off+2);
    ctx.payload = f+off+hlen;
    ctx.payload_len = end-off-hlen;
    return dp_engine_scan(&((struct inline_state *)state)->engines, &ctx);
}
