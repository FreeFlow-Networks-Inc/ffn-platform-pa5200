// SPDX-License-Identifier: GPL-2.0-or-later
/* Codec vectors and engine behaviour that need no Python. The protocol
 * conformance suite is octeon/debian/test_lacp_engine.py run with
 * FFN_LACP_ENGINE=native, which drives this engine through ffn_lacp_native.py. */
#include "ffn_lacp.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

/* Field-wise: the struct has padding and the decoder never promises its value. */
static bool same_info(const struct ffn_lacp_info *a, const struct ffn_lacp_info *b)
{
    return a->system_priority == b->system_priority && memcmp(a->system, b->system, 6) == 0 && a->key == b->key &&
           a->port_priority == b->port_priority && a->port == b->port && a->state == b->state;
}

static const uint8_t SYSTEM_A[6] = { 0x02, 0, 0, 0, 0, 1 };
static const uint8_t SYSTEM_B[6] = { 0x02, 0, 0, 0, 0, 2 };

struct fake { int fail; unsigned calls; struct ffn_lacp_gate last[FFN_LACP_MEMBERS]; unsigned count; int mangle; };

static int fake_apply(void *ctx, const uint16_t *ports, struct ffn_lacp_gate *gates, unsigned count, char *err, size_t errsz)
{
    struct fake *f = ctx;
    (void)ports;
    f->calls++;
    if (f->fail) { snprintf(err, errsz, "gate failed"); return -1; }
    if (f->mangle) gates[0].collect = !gates[0].collect;
    memcpy(f->last, gates, count * sizeof *gates); f->count = count;
    return 0;
}

static struct ffn_lacp *engine(const uint8_t system[6], struct fake *f, bool active, bool fast, unsigned min_links)
{
    struct ffn_lacp_config c;
    char err[128];
    memset(&c, 0, sizeof c);
    memcpy(c.system, system, 6);
    c.key = 1; c.system_priority = 32768; c.active = active; c.fast = fast; c.min_links = min_links; c.collecting = true;
    c.count = 2;
    c.members[0].port = 1; memcpy(c.members[0].source, system, 6);
    c.members[1].port = 2; memcpy(c.members[1].source, system, 6);
    struct ffn_lacp *e = ffn_lacp_create(&c, fake_apply, f, err, sizeof err);
    assert(e);
    return e;
}

/* Exchange LACPDUs between two engines every 100 ms over [start, end) seconds. */
static uint64_t run(struct ffn_lacp *a, struct ffn_lacp *b, unsigned start, unsigned end, int mute_port)
{
    uint64_t now = 0;
    for (unsigned n = start * 10; n < end * 10; n++) {
        now = (uint64_t)n * 100000;
        for (unsigned i = 0; i < 2; i++) {
            struct ffn_lacp *e = i ? b : a;
            assert(ffn_lacp_link(e, 1, true, 10000, now, 3000000) == 0);
            assert(ffn_lacp_link(e, 2, true, 10000, now, 3000000) == 0);
        }
        for (unsigned i = 0; i < 2; i++) {
            struct ffn_lacp *src = i ? b : a, *dst = i ? a : b;
            struct ffn_lacp_transmit out[FFN_LACP_MEMBERS];
            int n = ffn_lacp_transmissions(src, now, out, FFN_LACP_MEMBERS);
            assert(n >= 0);
            for (int k = 0; k < n; k++)
                if (!(src == b && out[k].port == mute_port))
                    assert(ffn_lacp_receive(dst, out[k].port, out[k].frame, FFN_LACP_PDU, now) >= 0);
        }
    }
    return now;
}

static void test_codec(void)
{
    struct ffn_lacp_info actor = { 32768, { 0x02, 0, 0, 0, 0, 1 }, 1, 32768, 1, 0x07 }, partner = { 0 }, back;
    struct ffn_lacp_pdu pdu;
    uint8_t frame[FFN_LACP_PDU], bad[FFN_LACP_PDU];
    static const uint8_t head[16] = { 0x01, 0x80, 0xc2, 0, 0, 0x02, 0x02, 0, 0, 0, 0, 1, 0x88, 0x09, 0x01, 0x01 };
    static const uint8_t actor_tlv[20] = { 0x01, 0x14, 0x80, 0x00, 0x02, 0, 0, 0, 0, 1, 0x00, 0x01, 0x80, 0x00, 0x00, 0x01, 0x07, 0, 0, 0 };
    assert(ffn_lacp_encode(&actor, &partner, SYSTEM_A, frame) == 0);
    assert(memcmp(frame, head, 16) == 0);
    assert(memcmp(frame + 16, actor_tlv, 20) == 0);
    assert(frame[36] == 2 && frame[37] == 20 && frame[56] == 3 && frame[57] == 16);
    for (unsigned i = 58; i < FFN_LACP_PDU; i++) assert(frame[i] == 0);
    assert(ffn_lacp_decode(frame, sizeof frame, &pdu) == 0);
    back = pdu.actor;
    assert(same_info(&back, &actor) && same_info(&pdu.partner, &partner));
    assert(memcmp(pdu.source, SYSTEM_A, 6) == 0 && pdu.collector_max_delay == 0);
    assert(ffn_lacp_decode(frame, FFN_LACP_PDU - 1, &pdu) == -1);
    memcpy(bad, frame, sizeof bad); bad[0] = 0x03; assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1);
    memcpy(bad, frame, sizeof bad); bad[6] |= 1; assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1);
    memcpy(bad, frame, sizeof bad); bad[15] = 2; assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1);
    memcpy(bad, frame, sizeof bad); bad[17] = 19; assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1);
    memcpy(bad, frame, sizeof bad); bad[31] = 0; assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1); /* actor port 0 */
    memcpy(bad, frame, sizeof bad); memset(bad + 20, 0, 6); assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1);
    memcpy(bad, frame, sizeof bad); bad[73] = 1; assert(ffn_lacp_decode(bad, sizeof bad, &pdu) == -1);
    assert(ffn_lacp_encode(&actor, &partner, (const uint8_t[6]){ 0x01, 0, 0, 0, 0, 1 }, frame) == -1);
    printf("codec ok\n");
}

static void test_negotiation(void)
{
    struct fake fa = { 0 }, fb = { 0 };
    struct ffn_lacp *a = engine(SYSTEM_A, &fa, true, true, 1), *b = engine(SYSTEM_B, &fb, true, true, 1);
    uint16_t ports[FFN_LACP_MEMBERS];
    struct ffn_lacp_transmit out[FFN_LACP_MEMBERS];
    struct ffn_lacp_pdu pdu;
    struct ffn_lacp_member_status m;
    assert(fa.calls == 1 && fa.count == 2 && !fa.last[0].collect && !fa.last[1].distribute);
    uint64_t now = run(a, b, 0, 10, 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 2 && ports[0] == 1 && ports[1] == 2);
    assert(ffn_lacp_distributing(b, ports, 8) == 2);
    assert(ffn_lacp_gates_verified(a) && fa.last[0].collect && fa.last[0].distribute);
    int n = ffn_lacp_transmissions(a, now + 1000000, out, 8);
    assert(n == 2);
    assert(ffn_lacp_decode(out[0].frame, FFN_LACP_PDU, &pdu) == 0);
    assert((pdu.actor.state & (FFN_LACP_SYNC | FFN_LACP_COLLECTING | FFN_LACP_DISTRIBUTING)) ==
           (FFN_LACP_SYNC | FFN_LACP_COLLECTING | FFN_LACP_DISTRIBUTING));
    /* Losing only PDUs on member 1 withdraws it before it defaults, then recovers. */
    now = run(a, b, 11, 14, 1);
    assert(ffn_lacp_distributing(a, ports, 8) == 1 && ports[0] == 2);
    assert(ffn_lacp_member(a, 1, &m) == 0 && m.rx == FFN_LACP_RX_EXPIRED);
    now = run(a, b, 14, 18, 1);
    assert(ffn_lacp_member(a, 1, &m) == 0 && m.rx == FFN_LACP_RX_DEFAULTED);
    now = run(a, b, 18, 26, 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 2);
    /* Carrier loss withdraws; the clock never runs backwards. */
    assert(ffn_lacp_link(a, 1, false, 0, now, 3000000) == 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 1 && ports[0] == 2);
    errno = 0;
    assert(ffn_lacp_tick(a, now - 1) == -1 && errno == EINVAL);
    assert(ffn_lacp_link(a, 1, true, 0, now, 3000000) == -1);
    assert(ffn_lacp_stop(a, now) == 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 0 && ffn_lacp_gates_verified(a));
    ffn_lacp_destroy(a); ffn_lacp_destroy(b);
    printf("negotiation ok\n");
}

static void test_passive_and_min_links(void)
{
    struct fake fa = { 0 }, fb = { 0 };
    struct ffn_lacp *a = engine(SYSTEM_A, &fa, false, true, 1), *b = engine(SYSTEM_B, &fb, false, true, 1);
    uint16_t ports[8];
    struct ffn_lacp_member_status m;
    run(a, b, 0, 10, 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 0);
    assert(ffn_lacp_member(a, 1, &m) == 0 && m.tx_attempts == 0);
    ffn_lacp_destroy(b);
    fb.calls = 0;
    b = engine(SYSTEM_B, &fb, true, true, 1);
    run(a, b, 10, 20, 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 2);
    ffn_lacp_destroy(a); ffn_lacp_destroy(b);
    struct fake fc = { 0 }, fd = { 0 };
    a = engine(SYSTEM_A, &fc, true, true, 2); b = engine(SYSTEM_B, &fd, true, true, 1);
    uint64_t now = run(a, b, 0, 10, 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 2);
    assert(ffn_lacp_link(a, 1, false, 0, now, 3000000) == 0);
    assert(ffn_lacp_distributing(a, ports, 8) == 0);
    ffn_lacp_destroy(a); ffn_lacp_destroy(b);
    printf("passive and minimum links ok\n");
}

static void test_faults(void)
{
    struct fake fa = { 0 }, fb = { 0 };
    struct ffn_lacp *a, *b;
    uint16_t ports[8];
    struct ffn_lacp_info actor;
    fa.fail = 1;
    a = engine(SYSTEM_A, &fa, true, true, 1);
    assert(ffn_lacp_fault(a) && strcmp(ffn_lacp_fault(a), "gate failed") == 0 && !ffn_lacp_gates_verified(a));
    b = engine(SYSTEM_B, &fb, true, true, 1);
    unsigned calls = fa.calls;
    uint64_t now = run(a, b, 0, 10, 0);
    assert(fa.calls == calls && ffn_lacp_distributing(a, ports, 8) == 0);
    assert(ffn_lacp_actor(a, 1, &actor) == 0 && !(actor.state & (FFN_LACP_COLLECTING | FFN_LACP_DISTRIBUTING)));
    fa.fail = 0;
    assert(ffn_lacp_stop(a, now) == 0);
    assert(ffn_lacp_gates_verified(a) && ffn_lacp_fault(a));
    ffn_lacp_destroy(a); ffn_lacp_destroy(b);
    struct fake fm = { 0 };
    fm.mangle = 1;
    a = engine(SYSTEM_A, &fm, true, true, 1);
    assert(ffn_lacp_fault(a) && strstr(ffn_lacp_fault(a), "readback") && !ffn_lacp_gates_verified(a));
    ffn_lacp_destroy(a);
    struct fake fo = { 0 };
    a = engine(SYSTEM_A, &fo, true, true, 1);
    assert(ffn_lacp_abort(a, "", 5) == 0 && strcmp(ffn_lacp_fault(a), "LACP owner failed") == 0);
    ffn_lacp_destroy(a);
    struct ffn_lacp_config c;
    char err[64];
    memset(&c, 0, sizeof c);
    memcpy(c.system, SYSTEM_A, 6); c.key = 1; c.system_priority = 1; c.min_links = 1; c.count = 1;
    c.members[0].port = 1; memcpy(c.members[0].source, SYSTEM_A, 6);
    c.system[0] = 0x01;
    assert(!ffn_lacp_create(&c, fake_apply, &fo, err, sizeof err) && strstr(err, "system MAC"));
    c.system[0] = 0x02; c.key = 0;
    assert(!ffn_lacp_create(&c, fake_apply, &fo, err, sizeof err) && strstr(err, "key"));
    printf("faults ok\n");
}

int main(void)
{
    test_codec();
    test_negotiation();
    test_passive_and_min_links();
    test_faults();
    printf("PASS: all LACP tests\n");
    return 0;
}
