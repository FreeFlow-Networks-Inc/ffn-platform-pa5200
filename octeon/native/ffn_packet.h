/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef FFN_PACKET_H
#define FFN_PACKET_H
#include <stdint.h>
#include <stddef.h>
#include "ffn_fe100_punt.h"
#define FFN_PACKET_ABI 2
#define FFN_PACKET_COUNTERS 16
#define FFN_PACKET_LOCAL_MAX 64
#define FFN_PACKET_LANES_MAX 8
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
/* Additive: lanes. Each lane is one receive socket in the trunk's fanout group
 * (demultiplexed by the receive queue the trunk driver records, the flow's SSO
 * group), one TAP queue and one transmit socket, with its own two workers, so
 * flows spread over lanes and stay in order within one. ffn_packet_lanes grows
 * an opened owner before it starts and returns the count reached (1 when the
 * TAP was not made with multi_queue); ffn_packet_adopt_lane adds a lane from
 * caller-owned descriptors; ffn_packet_start_lanes pins lane i's workers to
 * rx_cpus[i] and tx_cpus[i]; ffn_packet_lane_workers reports one lane
 * (started, rx cpu, tx cpu, rx tid, tx tid). */
int ffn_packet_lanes(struct ffn_packet *, unsigned);
int ffn_packet_adopt_lane(struct ffn_packet *, int, int, int);
unsigned ffn_packet_lane_count(const struct ffn_packet *);
int ffn_packet_start_lanes(struct ffn_packet *, const int *, const int *, unsigned);
int ffn_packet_lane_workers(struct ffn_packet *, unsigned, int *, unsigned);
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
/* The receive filter an owner with these source system ports and return bindings
 * attaches (mode 1 physical data, 2 aggregate control, 3 aggregate data), on any
 * datagram socket: the chip's returns reach only the owner of the front port named
 * in their packet info. */
int ffn_packet_filter_attach(int, const uint32_t *, unsigned, int,
    const struct ffn_packet_fe100_binding *, unsigned);
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
/* Additive control receiver. Same immutable member mapping as the data owner.
 * FE100 bindings are opt-in and expire independently; ordinary LACP survives. */
int ffn_aggregate_control_fe100(int, const struct ffn_aggregate_member *, unsigned,
    const struct ffn_packet_fe100_binding *, unsigned, uint64_t);
int ffn_aggregate_control_receive(int, const struct ffn_aggregate_member *, unsigned,
    const struct ffn_packet_fe100_binding *, unsigned, uint64_t, uint8_t *, size_t);
int ffn_aggregate_network(struct ffn_packet *, const struct ffn_aggregate_unit *, unsigned);
int ffn_aggregate_gates(struct ffn_packet *, unsigned, unsigned, unsigned,
    uint64_t, uint64_t, unsigned, void *, ffn_scan_fn);
int ffn_aggregate_stats(struct ffn_packet *, uint64_t *, unsigned);
int ffn_aggregate_member_stats(struct ffn_packet *, unsigned, uint64_t *, unsigned);
int ffn_aggregate_unit_stats(struct ffn_packet *, unsigned, uint64_t *, unsigned);
#endif
