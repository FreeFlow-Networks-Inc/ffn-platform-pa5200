/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef FFN_PACKET_H
#define FFN_PACKET_H
#include <stdint.h>
#include <stddef.h>
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
void ffn_packet_close(struct ffn_packet *);
#endif
