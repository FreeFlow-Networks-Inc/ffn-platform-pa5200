/* SPDX-License-Identifier: GPL-2.0-or-later */
/* LACP (IEEE 802.1AX) negotiation engine and LACPDU codec for the dataplane
 * aggregate owner. Transport independent: the owner supplies monotonic time,
 * leased carrier/speed observations and received Ethernet frames, and a gate
 * driver that programs collection/distribution per member and returns what it
 * read back. Nothing here opens a socket, infers carrier or touches hardware.
 *
 * This is the C form of octeon/debian/ffn_lacp_engine.py and
 * ffn_lacp_packets.py, state for state: the Python suites run against it
 * through octeon/debian/ffn_lacp_native.py as conformance tests. Times are
 * microseconds of one monotonic clock. */
#ifndef FFN_LACP_H
#define FFN_LACP_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define FFN_LACP_MEMBERS 8
#define FFN_LACP_PDU 124          /* an LACPDU frame as this engine builds it, without FCS */
#define FFN_LACP_FAULT 256

/* Actor/partner state bits. */
#define FFN_LACP_ACTIVITY     0x01
#define FFN_LACP_TIMEOUT      0x02
#define FFN_LACP_AGGREGATION  0x04
#define FFN_LACP_SYNC         0x08
#define FFN_LACP_COLLECTING   0x10
#define FFN_LACP_DISTRIBUTING 0x20
#define FFN_LACP_DEFAULTED    0x40
#define FFN_LACP_EXPIRED      0x80

/* One actor or partner information TLV body. */
struct ffn_lacp_info {
    uint16_t system_priority;
    uint8_t system[6];
    uint16_t key;
    uint16_t port_priority;
    uint16_t port;
    uint8_t state;
};

struct ffn_lacp_pdu {
    uint8_t source[6];
    struct ffn_lacp_info actor, partner;
    uint16_t collector_max_delay;
};

/* Strict decode of an untagged LACP version 1 frame. 0, or -1 for anything
 * the engine must count as invalid. Reserved octets and padding are ignored. */
int ffn_lacp_decode(const uint8_t *frame, size_t len, struct ffn_lacp_pdu *out);
/* Build the frame this engine transmits: FFN_LACP_PDU bytes into out. -1 when
 * the source is not a unicast MAC. */
int ffn_lacp_encode(const struct ffn_lacp_info *actor, const struct ffn_lacp_info *partner,
                    const uint8_t source[6], uint8_t out[FFN_LACP_PDU]);

struct ffn_lacp_gate { bool collect, distribute; };

/* The gate driver. gates[] holds the desired state on entry and must hold the
 * state read back from the data path on return; ports[] names the member of
 * each entry in the engine's member order. Return 0 on success. On failure
 * return nonzero and optionally write a message into err (empty means the
 * engine records "apply failed"). The engine latches a fault when the call
 * fails or the readback differs from the request. */
typedef int (*ffn_lacp_apply_fn)(void *ctx, const uint16_t *ports, struct ffn_lacp_gate *gates,
                                 unsigned count, char *err, size_t errsz);

struct ffn_lacp_member_config { uint16_t port; uint8_t source[6]; };

struct ffn_lacp_config {
    uint8_t system[6];          /* unicast system MAC */
    uint16_t key;               /* 1..65535 */
    uint16_t system_priority;   /* 1..65535 */
    bool active;                /* LACP activity: active or passive */
    bool fast;                  /* short timeout: request the peer transmits every second */
    unsigned min_links;         /* 1..count */
    bool collecting;            /* false: negotiate only, never open a gate */
    unsigned count;             /* 1..FFN_LACP_MEMBERS */
    struct ffn_lacp_member_config members[FFN_LACP_MEMBERS];
};

struct ffn_lacp;

/* Validates the configuration and closes every gate through the driver before
 * returning. NULL with err on an invalid configuration. A driver failure at
 * this point does not fail creation: it latches a fault, as the owner's loop
 * expects. */
struct ffn_lacp *ffn_lacp_create(const struct ffn_lacp_config *, ffn_lacp_apply_fn, void *ctx,
                                 char *err, size_t errsz);
void ffn_lacp_destroy(struct ffn_lacp *);

/* Every call returns -1 for an unknown port, an invalid argument or a
 * monotonic time earlier than one already seen (the engine's clock never runs
 * backwards; an owner that needs the reason reads errno == EINVAL). */
/* A fresh carrier/speed observation, valid for lease_us (0 < lease <= 30 s). */
int ffn_lacp_link(struct ffn_lacp *, uint16_t port, bool up, uint32_t speed_mbps,
                  uint64_t now_us, uint64_t lease_us);
/* 1 when the frame advanced the member's receive state, 0 when it was ignored
 * or counted invalid. */
int ffn_lacp_receive(struct ffn_lacp *, uint16_t port, const uint8_t *frame, size_t len,
                     uint64_t now_us);
int ffn_lacp_tick(struct ffn_lacp *, uint64_t now_us);
/* Advances timers and yields the LACPDUs due now, at most one per member,
 * never more than three per member per second. Returns the count. */
struct ffn_lacp_transmit { uint16_t port; uint8_t frame[FFN_LACP_PDU]; };
int ffn_lacp_transmissions(struct ffn_lacp *, uint64_t now_us, struct ffn_lacp_transmit *out,
                           unsigned max);
/* Withdraws every member and closes every gate; retried even after a fault. */
int ffn_lacp_stop(struct ffn_lacp *, uint64_t now_us);
/* Latches an owner/transport failure (reason, or a default) and stops. */
int ffn_lacp_abort(struct ffn_lacp *, const char *reason, uint64_t now_us);

/* Observation. */
const char *ffn_lacp_fault(const struct ffn_lacp *);           /* NULL while healthy */
bool ffn_lacp_gates_verified(const struct ffn_lacp *);         /* the driver's last readback matched */
unsigned ffn_lacp_count(const struct ffn_lacp *);
int ffn_lacp_port_at(const struct ffn_lacp *, unsigned index);  /* member order, -1 past the end */
int ffn_lacp_actor(const struct ffn_lacp *, uint16_t port, struct ffn_lacp_info *out);
/* Distributing members in member order (empty while faulted). */
unsigned ffn_lacp_distributing(const struct ffn_lacp *, uint16_t *ports, unsigned max);
/* The gate map last read back, in member order: 0 and the gates, or -1 when
 * nothing is verified. */
int ffn_lacp_applied(const struct ffn_lacp *, struct ffn_lacp_gate *gates, unsigned max);

enum ffn_lacp_rx { FFN_LACP_RX_DISABLED, FFN_LACP_RX_CURRENT, FFN_LACP_RX_EXPIRED, FFN_LACP_RX_DEFAULTED };
const char *ffn_lacp_rx_name(enum ffn_lacp_rx);

struct ffn_lacp_member_status {
    uint16_t port;
    enum ffn_lacp_rx rx;
    bool link;
    uint32_t speed_mbps;
    uint64_t lease_us, deadline_us;
    bool selected, matched, sync, dirty;
    bool has_peer;
    struct ffn_lacp_info peer;
    uint64_t tx_attempts, received, invalid;
    bool has_gate;
    struct ffn_lacp_gate gate;
};
int ffn_lacp_member(const struct ffn_lacp *, uint16_t port, struct ffn_lacp_member_status *out);
/* Request an immediate advertisement on the member's next transmission slot. */
int ffn_lacp_mark_dirty(struct ffn_lacp *, uint16_t port);
/* The last monotonic time the engine saw; 0 before the first call. */
uint64_t ffn_lacp_now(const struct ffn_lacp *);

#endif
