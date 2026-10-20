// SPDX-License-Identifier: GPL-2.0-or-later
/* LACP negotiation engine and LACPDU codec. See ffn_lacp.h.
 *
 * Every rule here has a twin in octeon/debian/ffn_lacp_engine.py; keep them in
 * step, the Python suites are the conformance tests for this file. */
#include "ffn_lacp.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SECOND 1000000ULL
#define RECEIVE_DEADLINE_FAST (3 * SECOND)
#define RECEIVE_DEADLINE_SLOW (90 * SECOND)
#define EXPIRY_GRACE (3 * SECOND)
#define AGGREGATION_WAIT (2 * SECOND)
#define LEASE_MAX (30 * SECOND)
#define INTERVAL_FAST (1 * SECOND)
#define INTERVAL_SLOW (30 * SECOND)

static const uint8_t destination[6] = { 0x01, 0x80, 0xc2, 0x00, 0x00, 0x02 };
static const struct ffn_lacp_info zero_info;

struct member {
    uint16_t port;
    uint8_t source[6];
    bool link;
    uint32_t speed;
    uint64_t lease;
    bool has_peer, has_echo;
    struct ffn_lacp_info peer, echo;
    enum ffn_lacp_rx rx;
    uint64_t deadline;
    bool selected, matched, sync, dirty;
    uint64_t selected_since;
    bool has_last_tx, has_last_pdu;
    uint64_t last_tx;
    uint8_t last_pdu[FFN_LACP_PDU];
    uint64_t tx, rx_count, invalid;
};

struct bucket { uint16_t priority; uint8_t system[6]; uint16_t key; uint32_t speed; };

struct ffn_lacp {
    uint8_t system[6];
    uint16_t key, priority;
    bool active, fast, collecting;
    unsigned min_links, count;
    struct member m[FFN_LACP_MEMBERS];
    bool has_bucket;
    struct bucket bucket;
    bool has_now;
    uint64_t now;
    bool faulted;
    char fault[FFN_LACP_FAULT];
    bool has_applied;
    struct ffn_lacp_gate applied[FFN_LACP_MEMBERS];
    ffn_lacp_apply_fn apply;
    void *ctx;
};

/* ------------------------------------------------------------------ codec */

static bool unicast(const uint8_t mac[6])
{
    static const uint8_t none[6];
    return !(mac[0] & 1) && memcmp(mac, none, 6) != 0;
}

static uint16_t be16(const uint8_t *p) { return (uint16_t)(p[0] << 8 | p[1]); }
static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }

static void info_decode(const uint8_t *p, struct ffn_lacp_info *out)
{
    out->system_priority = be16(p);
    memcpy(out->system, p + 2, 6);
    out->key = be16(p + 8);
    out->port_priority = be16(p + 10);
    out->port = be16(p + 12);
    out->state = p[14];
}

static void info_encode(uint8_t *p, uint8_t kind, const struct ffn_lacp_info *in)
{
    p[0] = kind; p[1] = 20;
    put16(p + 2, in->system_priority);
    memcpy(p + 4, in->system, 6);
    put16(p + 10, in->key);
    put16(p + 12, in->port_priority);
    put16(p + 14, in->port);
    p[16] = in->state;
    p[17] = p[18] = p[19] = 0;
}

int ffn_lacp_decode(const uint8_t *frame, size_t len, struct ffn_lacp_pdu *out)
{
    struct ffn_lacp_pdu pdu;
    if (!frame || !out || len < FFN_LACP_PDU) return -1;
    memset(&pdu, 0, sizeof pdu);
    if (memcmp(frame, destination, 6) != 0) return -1;
    if (frame[12] != 0x88 || frame[13] != 0x09 || frame[14] != 0x01 || frame[15] != 0x01) return -1;
    if (!unicast(frame + 6)) return -1;
    if (frame[16] != 1 || frame[17] != 20 || frame[36] != 2 || frame[37] != 20) return -1;
    if (frame[56] != 3 || frame[57] != 16 || frame[72] != 0 || frame[73] != 0) return -1;
    memcpy(pdu.source, frame + 6, 6);
    info_decode(frame + 18, &pdu.actor);
    info_decode(frame + 38, &pdu.partner);
    pdu.collector_max_delay = be16(frame + 58);
    if (!unicast(pdu.actor.system) || pdu.actor.port == 0) return -1;
    *out = pdu;
    return 0;
}

int ffn_lacp_encode(const struct ffn_lacp_info *actor, const struct ffn_lacp_info *partner,
                    const uint8_t source[6], uint8_t out[FFN_LACP_PDU])
{
    if (!actor || !partner || !source || !out || !unicast(source)) return -1;
    memset(out, 0, FFN_LACP_PDU);
    memcpy(out, destination, 6);
    memcpy(out + 6, source, 6);
    out[12] = 0x88; out[13] = 0x09; out[14] = 0x01; out[15] = 0x01;
    info_encode(out + 16, 1, actor);
    info_encode(out + 36, 2, partner);
    out[56] = 0x03; out[57] = 0x10;
    return 0;
}

/* ----------------------------------------------------------------- engine */

static bool same_identity(const struct ffn_lacp_info *a, const struct ffn_lacp_info *b)
{
    return a->system_priority == b->system_priority && memcmp(a->system, b->system, 6) == 0 &&
           a->key == b->key && a->port_priority == b->port_priority && a->port == b->port;
}

static struct member *find(struct ffn_lacp *e, uint16_t port)
{
    for (unsigned i = 0; i < e->count; i++)
        if (e->m[i].port == port) return &e->m[i];
    return NULL;
}

static int set_time(struct ffn_lacp *e, uint64_t now)
{
    if (e->has_now && now < e->now) { errno = EINVAL; return -1; }
    e->has_now = true;
    e->now = now;
    return 0;
}

static void closed(const struct ffn_lacp *e, struct ffn_lacp_gate *gates)
{
    for (unsigned i = 0; i < e->count; i++) gates[i].collect = gates[i].distribute = false;
}

static bool gates_equal(const struct ffn_lacp *e, const struct ffn_lacp_gate *a, const struct ffn_lacp_gate *b)
{
    for (unsigned i = 0; i < e->count; i++)
        if (a[i].collect != b[i].collect || a[i].distribute != b[i].distribute) return false;
    return true;
}

static void latch(struct ffn_lacp *e, const char *message)
{
    e->faulted = true;
    snprintf(e->fault, sizeof e->fault, "%s", message && message[0] ? message : "apply failed");
}

/* Best-effort withdrawal after a failure: the driver may still close every
 * gate, and only an exact closed readback is recorded. */
static void withdraw(struct ffn_lacp *e)
{
    struct ffn_lacp_gate off[FFN_LACP_MEMBERS], gates[FFN_LACP_MEMBERS];
    uint16_t ports[FFN_LACP_MEMBERS];
    char err[FFN_LACP_FAULT] = "";
    for (unsigned i = 0; i < e->count; i++) ports[i] = e->m[i].port;
    closed(e, off);
    memcpy(gates, off, sizeof gates);
    if (e->apply(e->ctx, ports, gates, e->count, err, sizeof err) == 0 && gates_equal(e, gates, off)) {
        memcpy(e->applied, off, sizeof e->applied);
        e->has_applied = true;
    }
}

static void apply(struct ffn_lacp *e, const struct ffn_lacp_gate *desired)
{
    struct ffn_lacp_gate gates[FFN_LACP_MEMBERS];
    uint16_t ports[FFN_LACP_MEMBERS];
    char err[FFN_LACP_FAULT] = "";
    if (e->faulted) return;
    if (e->has_applied && gates_equal(e, desired, e->applied)) return;
    for (unsigned i = 0; i < e->count; i++) ports[i] = e->m[i].port;
    memcpy(gates, desired, e->count * sizeof *gates);
    if (e->apply(e->ctx, ports, gates, e->count, err, sizeof err) != 0) {
        latch(e, err);
    } else if (!gates_equal(e, gates, desired)) {
        latch(e, "Member gate readback mismatch");
    } else {
        memcpy(e->applied, gates, e->count * sizeof *gates);
        e->has_applied = true;
        return;
    }
    /* A failed withdrawal remains uncertain, requiring owner recovery rather
     * than silently retrying enables. */
    e->has_applied = false;
    withdraw(e);
}

static int validate(const struct ffn_lacp_config *c, char *err, size_t errsz)
{
    const char *why = NULL;
    if (!c) why = "missing configuration";
    else if (!unicast(c->system)) why = "Unicast system MAC required";
    else if (c->key == 0) why = "Invalid actor key";
    else if (c->system_priority == 0) why = "Invalid system priority";
    else if (c->count < 1 || c->count > FFN_LACP_MEMBERS) why = "Invalid members";
    else if (c->min_links < 1 || c->min_links > c->count) why = "Invalid minimum links";
    for (unsigned i = 0; !why && i < c->count; i++) {
        if (c->members[i].port == 0) why = "Invalid members";
        else if (!unicast(c->members[i].source)) why = "Unicast member MAC required";
        for (unsigned j = 0; !why && j < i; j++)
            if (c->members[j].port == c->members[i].port) why = "Invalid members";
    }
    if (why) { if (err && errsz) snprintf(err, errsz, "%s", why); return -1; }
    return 0;
}

struct ffn_lacp *ffn_lacp_create(const struct ffn_lacp_config *c, ffn_lacp_apply_fn fn, void *ctx,
                                 char *err, size_t errsz)
{
    struct ffn_lacp *e;
    struct ffn_lacp_gate off[FFN_LACP_MEMBERS];
    if (err && errsz) err[0] = 0;
    if (validate(c, err, errsz)) return NULL;
    if (!fn) { if (err && errsz) snprintf(err, errsz, "gate driver required"); return NULL; }
    e = calloc(1, sizeof *e);
    if (!e) { if (err && errsz) snprintf(err, errsz, "out of memory"); return NULL; }
    memcpy(e->system, c->system, 6);
    e->key = c->key; e->priority = c->system_priority;
    e->active = c->active; e->fast = c->fast; e->collecting = c->collecting;
    e->min_links = c->min_links; e->count = c->count;
    for (unsigned i = 0; i < c->count; i++) {
        struct member *m = &e->m[i];
        m->port = c->members[i].port;
        memcpy(m->source, c->members[i].source, 6);
        m->rx = FFN_LACP_RX_DISABLED;
        m->dirty = true;
    }
    e->apply = fn; e->ctx = ctx;
    /* Initialization must close every gate before any LACPDU is advertised. */
    closed(e, off);
    apply(e, off);
    return e;
}

void ffn_lacp_destroy(struct ffn_lacp *e) { free(e); }

static void actor_of(const struct ffn_lacp *e, const struct member *m, struct ffn_lacp_info *out)
{
    unsigned index = (unsigned)(m - e->m);
    uint8_t state = FFN_LACP_AGGREGATION | (e->active ? FFN_LACP_ACTIVITY : 0) | (e->fast ? FFN_LACP_TIMEOUT : 0);
    if (m->rx == FFN_LACP_RX_DEFAULTED) state |= FFN_LACP_DEFAULTED;
    if (m->rx == FFN_LACP_RX_EXPIRED) state |= FFN_LACP_EXPIRED;
    if (m->sync && !e->faulted) state |= FFN_LACP_SYNC;
    if (e->has_applied && !e->faulted) {
        if (e->applied[index].collect) state |= FFN_LACP_COLLECTING;
        if (e->applied[index].distribute) state |= FFN_LACP_DISTRIBUTING;
    }
    out->system_priority = e->priority;
    memcpy(out->system, e->system, 6);
    out->key = e->key;
    out->port_priority = 32768;
    out->port = m->port;
    out->state = state;
}

static int bucket_compare(const struct bucket *a, unsigned na, const struct bucket *b, unsigned nb)
{
    /* Python: min(buckets, key=(-len*speed, priority, system, key, speed)). */
    uint64_t wa = (uint64_t)na * a->speed, wb = (uint64_t)nb * b->speed;
    int c;
    if (wa != wb) return wa > wb ? -1 : 1;
    if (a->priority != b->priority) return a->priority < b->priority ? -1 : 1;
    c = memcmp(a->system, b->system, 6);
    if (c) return c < 0 ? -1 : 1;
    if (a->key != b->key) return a->key < b->key ? -1 : 1;
    if (a->speed != b->speed) return a->speed < b->speed ? -1 : 1;
    return 0;
}

static bool bucket_equal(const struct bucket *a, const struct bucket *b)
{
    return a->priority == b->priority && memcmp(a->system, b->system, 6) == 0 && a->key == b->key && a->speed == b->speed;
}

static void tick(struct ffn_lacp *e, uint64_t now)
{
    struct bucket keys[FFN_LACP_MEMBERS];
    unsigned sizes[FFN_LACP_MEMBERS], nkeys = 0;
    bool in_bucket[FFN_LACP_MEMBERS] = { false };
    struct ffn_lacp_gate desired[FFN_LACP_MEMBERS];
    uint16_t eligible[FFN_LACP_MEMBERS];
    unsigned neligible = 0, i, j;
    for (i = 0; i < e->count; i++) {
        struct member *m = &e->m[i];
        if (!m->link || now >= m->lease) {
            m->link = false; m->rx = FFN_LACP_RX_DISABLED;
            m->has_peer = m->has_echo = false; m->matched = false;
        } else if (m->rx == FFN_LACP_RX_CURRENT && now >= m->deadline) {
            m->rx = FFN_LACP_RX_EXPIRED; m->matched = false; m->deadline += EXPIRY_GRACE; m->dirty = true;
            if (m->has_peer) m->peer.state = (uint8_t)((m->peer.state & ~FFN_LACP_SYNC) | FFN_LACP_TIMEOUT);
        }
        if (m->rx == FFN_LACP_RX_EXPIRED && now >= m->deadline) {
            m->rx = FFN_LACP_RX_DEFAULTED; m->has_peer = m->has_echo = false; m->matched = false; m->dirty = true;
        }
    }
    /* Selection: current peers advertising aggregation, excluding any partner
     * port identity seen on more than one member, grouped by partner system,
     * key and local speed. */
    for (i = 0; i < e->count; i++) {
        struct member *m = &e->m[i];
        struct bucket key;
        unsigned duplicates = 0;
        if (m->rx != FFN_LACP_RX_CURRENT || !m->has_peer || !(m->peer.state & FFN_LACP_AGGREGATION)) continue;
        for (j = 0; j < e->count; j++) {
            const struct member *o = &e->m[j];
            if (o->rx == FFN_LACP_RX_CURRENT && o->has_peer && memcmp(o->peer.system, m->peer.system, 6) == 0 &&
                o->peer.port == m->peer.port) duplicates++;
        }
        if (duplicates != 1) continue;
        key.priority = m->peer.system_priority; memcpy(key.system, m->peer.system, 6);
        key.key = m->peer.key; key.speed = m->speed;
        for (j = 0; j < nkeys; j++) if (bucket_equal(&keys[j], &key)) break;
        if (j == nkeys) { keys[nkeys] = key; sizes[nkeys] = 0; nkeys++; }
        sizes[j]++;
        in_bucket[i] = true;
    }
    if (e->has_bucket) {
        for (j = 0; j < nkeys; j++) if (bucket_equal(&keys[j], &e->bucket)) break;
        if (j == nkeys) e->has_bucket = false;
    }
    if (!e->has_bucket && nkeys) {
        unsigned best = 0;
        for (j = 1; j < nkeys; j++)
            if (bucket_compare(&keys[j], sizes[j], &keys[best], sizes[best]) < 0) best = j;
        e->bucket = keys[best]; e->has_bucket = true;
    }
    closed(e, desired);
    for (i = 0; i < e->count; i++) {
        struct member *m = &e->m[i];
        bool was = m->sync, selected = false;
        if (e->has_bucket && in_bucket[i]) {
            struct bucket key;
            key.priority = m->peer.system_priority; memcpy(key.system, m->peer.system, 6);
            key.key = m->peer.key; key.speed = m->speed;
            selected = bucket_equal(&key, &e->bucket);
        }
        if (selected && !m->selected) m->selected_since = now;
        m->selected = selected;
        m->sync = selected && now - m->selected_since >= AGGREGATION_WAIT && !e->faulted;
        if (was != m->sync) m->dirty = true;
        if (e->collecting && m->sync && m->matched && m->has_peer && (m->peer.state & FFN_LACP_SYNC)) {
            desired[i].collect = true;
            if (m->peer.state & FFN_LACP_COLLECTING) eligible[neligible++] = (uint16_t)i;
        }
    }
    if (neligible >= e->min_links)
        for (i = 0; i < neligible; i++) desired[eligible[i]].distribute = true;
    apply(e, desired);
}

int ffn_lacp_tick(struct ffn_lacp *e, uint64_t now)
{
    if (!e) { errno = EINVAL; return -1; }
    if (set_time(e, now)) return -1;
    tick(e, now);
    return 0;
}

int ffn_lacp_link(struct ffn_lacp *e, uint16_t port, bool up, uint32_t speed, uint64_t now, uint64_t lease)
{
    struct member *m;
    if (!e || (up && speed == 0) || lease == 0 || lease > LEASE_MAX) { errno = EINVAL; return -1; }
    if (set_time(e, now)) return -1;
    m = find(e, port);
    if (!m) { errno = EINVAL; return -1; }
    if (m->link != up || m->speed != speed) {
        m->has_peer = m->has_echo = false; m->matched = false; m->selected = false; m->sync = false;
        m->rx = up ? FFN_LACP_RX_EXPIRED : FFN_LACP_RX_DISABLED;
        m->deadline = now + EXPIRY_GRACE;
        m->dirty = true;
    }
    m->link = up; m->speed = speed; m->lease = now + lease;
    tick(e, now);
    return 0;
}

int ffn_lacp_receive(struct ffn_lacp *e, uint16_t port, const uint8_t *frame, size_t len, uint64_t now)
{
    struct member *m;
    struct ffn_lacp_pdu pdu;
    struct ffn_lacp_info actor;
    bool matched, dirty;
    if (!e || !frame) { errno = EINVAL; return -1; }
    if (set_time(e, now)) return -1;
    tick(e, now);
    m = find(e, port);
    if (!m) { errno = EINVAL; return -1; }
    if (!m->link || now >= m->lease || e->faulted) return 0;
    if (ffn_lacp_decode(frame, len, &pdu)) { m->invalid++; return 0; }
    if (memcmp(pdu.actor.system, e->system, 6) == 0) { m->invalid++; return 0; }
    if (!m->has_peer || !same_identity(&m->peer, &pdu.actor) ||
        !!(m->peer.state & FFN_LACP_AGGREGATION) != !!(pdu.actor.state & FFN_LACP_AGGREGATION)) {
        m->selected = false; m->sync = false; m->dirty = true;
    }
    actor_of(e, m, &actor);
    matched = same_identity(&pdu.partner, &actor) && (pdu.partner.state & FFN_LACP_AGGREGATION) &&
              ((pdu.actor.state & FFN_LACP_ACTIVITY) || (e->active && (pdu.partner.state & FFN_LACP_ACTIVITY)));
    /* Request an update only if our current advertisement or recorded peer
     * differs, not for every identical periodic receive. */
    dirty = !m->has_peer || !same_identity(&m->peer, &pdu.actor) || m->peer.state != pdu.actor.state ||
            !same_identity(&pdu.partner, &actor) ||
            ((pdu.partner.state ^ actor.state) & (FFN_LACP_ACTIVITY | FFN_LACP_TIMEOUT | FFN_LACP_AGGREGATION |
                                                  FFN_LACP_SYNC | FFN_LACP_COLLECTING | FFN_LACP_DISTRIBUTING));
    if (dirty) m->dirty = true;
    m->peer = pdu.actor; m->has_peer = true;
    m->echo = pdu.partner; m->has_echo = true;
    m->matched = matched;
    m->rx = FFN_LACP_RX_CURRENT;
    m->deadline = now + (e->fast ? RECEIVE_DEADLINE_FAST : RECEIVE_DEADLINE_SLOW);
    m->rx_count++;
    tick(e, now);
    return 1;
}

int ffn_lacp_transmissions(struct ffn_lacp *e, uint64_t now, struct ffn_lacp_transmit *out, unsigned max)
{
    unsigned n = 0;
    if (!e || (!out && max)) { errno = EINVAL; return -1; }
    if (set_time(e, now)) return -1;
    tick(e, now);
    for (unsigned i = 0; i < e->count && n < max; i++) {
        struct member *m = &e->m[i];
        const struct ffn_lacp_info *peer = m->has_peer ? &m->peer : &zero_info;
        struct ffn_lacp_info actor;
        uint8_t frame[FFN_LACP_PDU];
        uint64_t interval;
        bool changed, due;
        if (!m->link || e->faulted || !(e->active || (peer->state & FFN_LACP_ACTIVITY))) continue;
        interval = (!m->has_peer || (peer->state & FFN_LACP_TIMEOUT)) ? INTERVAL_FAST : INTERVAL_SLOW;
        actor_of(e, m, &actor);
        if (ffn_lacp_encode(&actor, peer, m->source, frame)) continue;
        changed = !m->has_last_pdu || memcmp(frame, m->last_pdu, FFN_LACP_PDU) != 0;
        /* Limit attempts as well as successful sends to <=3 PDUs/s. */
        due = !m->has_last_tx || (now - m->last_tx) * 3 >= SECOND;
        if (due && (m->dirty || changed || !m->has_last_tx || now - m->last_tx >= interval)) {
            m->last_tx = now; m->has_last_tx = true; m->dirty = false;
            memcpy(m->last_pdu, frame, FFN_LACP_PDU); m->has_last_pdu = true;
            m->tx++;
            out[n].port = m->port;
            memcpy(out[n].frame, frame, FFN_LACP_PDU);
            n++;
        }
    }
    return (int)n;
}

int ffn_lacp_stop(struct ffn_lacp *e, uint64_t now)
{
    struct ffn_lacp_gate off[FFN_LACP_MEMBERS];
    if (!e) { errno = EINVAL; return -1; }
    if (set_time(e, now)) return -1;
    for (unsigned i = 0; i < e->count; i++) {
        struct member *m = &e->m[i];
        m->link = false; m->rx = FFN_LACP_RX_DISABLED;
        m->has_peer = m->has_echo = false; m->sync = false; m->selected = false;
    }
    if (e->faulted) {
        /* Explicit owner shutdown retries withdrawal even after an earlier
         * driver failure. The fault stays latched; this cannot enable gates. */
        e->has_applied = false;
        withdraw(e);
    } else {
        closed(e, off);
        apply(e, off);
    }
    return 0;
}

int ffn_lacp_abort(struct ffn_lacp *e, const char *reason, uint64_t now)
{
    if (!e) { errno = EINVAL; return -1; }
    if (set_time(e, now)) return -1;
    if (!e->faulted) latch(e, reason && reason[0] ? reason : "LACP owner failed");
    return ffn_lacp_stop(e, now);
}

/* ------------------------------------------------------------ observation */

const char *ffn_lacp_fault(const struct ffn_lacp *e) { return e && e->faulted ? e->fault : NULL; }
bool ffn_lacp_gates_verified(const struct ffn_lacp *e) { return e && e->has_applied; }
unsigned ffn_lacp_count(const struct ffn_lacp *e) { return e ? e->count : 0; }
int ffn_lacp_port_at(const struct ffn_lacp *e, unsigned index) { return e && index < e->count ? e->m[index].port : -1; }
uint64_t ffn_lacp_now(const struct ffn_lacp *e) { return e && e->has_now ? e->now : 0; }

const char *ffn_lacp_rx_name(enum ffn_lacp_rx rx)
{
    switch (rx) {
    case FFN_LACP_RX_CURRENT: return "current";
    case FFN_LACP_RX_EXPIRED: return "expired";
    case FFN_LACP_RX_DEFAULTED: return "defaulted";
    default: return "disabled";
    }
}

int ffn_lacp_actor(const struct ffn_lacp *e, uint16_t port, struct ffn_lacp_info *out)
{
    const struct member *m = e ? find((struct ffn_lacp *)e, port) : NULL;
    if (!m || !out) { errno = EINVAL; return -1; }
    actor_of(e, m, out);
    return 0;
}

unsigned ffn_lacp_distributing(const struct ffn_lacp *e, uint16_t *ports, unsigned max)
{
    unsigned n = 0;
    if (!e || !e->has_applied || e->faulted) return 0;
    for (unsigned i = 0; i < e->count; i++)
        if (e->applied[i].distribute) { if (ports && n < max) ports[n] = e->m[i].port; n++; }
    return n;
}

int ffn_lacp_applied(const struct ffn_lacp *e, struct ffn_lacp_gate *gates, unsigned max)
{
    if (!e || !gates || max < e->count) { errno = EINVAL; return -1; }
    if (!e->has_applied) return -1;
    memcpy(gates, e->applied, e->count * sizeof *gates);
    return 0;
}

int ffn_lacp_member(const struct ffn_lacp *e, uint16_t port, struct ffn_lacp_member_status *out)
{
    const struct member *m = e ? find((struct ffn_lacp *)e, port) : NULL;
    if (!m || !out) { errno = EINVAL; return -1; }
    memset(out, 0, sizeof *out);
    out->port = m->port; out->rx = m->rx; out->link = m->link; out->speed_mbps = m->speed;
    out->lease_us = m->lease; out->deadline_us = m->deadline;
    out->selected = m->selected; out->matched = m->matched; out->sync = m->sync; out->dirty = m->dirty;
    out->has_peer = m->has_peer; if (m->has_peer) out->peer = m->peer;
    out->tx_attempts = m->tx; out->received = m->rx_count; out->invalid = m->invalid;
    out->has_gate = e->has_applied;
    if (e->has_applied) out->gate = e->applied[m - e->m];
    return 0;
}

int ffn_lacp_mark_dirty(struct ffn_lacp *e, uint16_t port)
{
    struct member *m = e ? find(e, port) : NULL;
    if (!m) { errno = EINVAL; return -1; }
    m->dirty = true;
    return 0;
}
