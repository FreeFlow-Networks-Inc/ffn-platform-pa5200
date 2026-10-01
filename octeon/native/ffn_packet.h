/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef FFN_PACKET_H
#define FFN_PACKET_H
#include <stdint.h>
#include <stddef.h>
#include "ffn_fe100_punt.h"
#define FFN_PACKET_ABI 2
#define FFN_PACKET_COUNTERS 16
#define FFN_PACKET_LOCAL_MAX 64
struct ffn_packet;
typedef int (*ffn_scan_fn)(void *, const char *, unsigned);
unsigned ffn_packet_abi(void);
struct ffn_packet *ffn_packet_open(const char *, const char *, const char *, unsigned);
/* Duplicate caller-owned descriptors; useful for isolated transport validation. */
struct ffn_packet *ffn_packet_adopt(int, int, int, unsigned);
int ffn_packet_configure(struct ffn_packet *, const uint8_t *, unsigned,
                         const uint8_t *, unsigned, void *, ffn_scan_fn);
int ffn_packet_poll(struct ffn_packet *, unsigned);
/* RX and TX keep ordered, independent workers. Start returns paused. */
int ffn_packet_start(struct ffn_packet *, int, int);
int ffn_packet_pause(struct ffn_packet *);
int ffn_packet_resume(struct ffn_packet *);
int ffn_packet_workers(struct ffn_packet *, int *, unsigned);
int ffn_packet_counters(struct ffn_packet *, uint64_t *, unsigned);
/* Additive ABI: receive syscalls, frames returned, largest batch. */
int ffn_packet_receive_stats(struct ffn_packet *, uint64_t *, unsigned);
/* Physical TX only: sendmmsg calls, accepted frames, largest accepted batch. */
int ffn_packet_transmit_stats(struct ffn_packet *, uint64_t *, unsigned);
/* Additive, opt-in exception attachment. Configure only while paused. The
 * monotonic deadline must be renewed by the trusted attachment controller;
 * expiry rejects punts without changing the normal software packet path. */
struct ffn_packet_fe100_binding {
    struct ffn_fe100_punt_scope scope;
    uint16_t source;
};
int ffn_packet_fe100(struct ffn_packet *, const struct ffn_packet_fe100_binding *,
    unsigned, uint64_t);
int ffn_packet_fe100_stats(struct ffn_packet *, uint64_t *, unsigned);
void ffn_packet_close(struct ffn_packet *);
/* Aggregate ABI 1: immutable member mapping, paused configuration updates. */
struct ffn_aggregate_member { uint32_t front, source, alias; };
struct ffn_aggregate_unit {
    uint32_t tag, mtu, n4, n6; /* tag 4096 is the untagged parent */
    uint8_t local4[32][4], local6[32][16];
};
unsigned ffn_aggregate_abi(void);
struct ffn_packet *ffn_aggregate_open(const char *, const char *, const char *,
    const struct ffn_aggregate_member *, unsigned, unsigned);
struct ffn_packet *ffn_aggregate_adopt(int, int, int,
    const struct ffn_aggregate_member *, unsigned, unsigned);
int ffn_aggregate_control_open(const char *, const uint32_t *, unsigned);
int ffn_aggregate_network(struct ffn_packet *, const struct ffn_aggregate_unit *, unsigned);
int ffn_aggregate_gates(struct ffn_packet *, unsigned, unsigned, unsigned,
    uint64_t, uint64_t, unsigned, void *, ffn_scan_fn);
int ffn_aggregate_stats(struct ffn_packet *, uint64_t *, unsigned);
int ffn_aggregate_member_stats(struct ffn_packet *, unsigned, uint64_t *, unsigned);
int ffn_aggregate_unit_stats(struct ffn_packet *, unsigned, uint64_t *, unsigned);
#endif
