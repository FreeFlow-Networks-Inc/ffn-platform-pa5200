/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef FFN_FE100_STATS_H
#define FFN_FE100_STATS_H
#include <stddef.h>
#include <stdint.h>
#define FFN_FE100_STATS_MAX 125
struct ffn_fe100_counter {
    uint32_t flow_id, octets;
    uint8_t packets, reason;
};
struct ffn_fe100_counters {
    unsigned count;
    struct ffn_fe100_counter records[FFN_FE100_STATS_MAX];
};
/* RAW BCM return, FLOWSTATS (19), compact FCR format 0. Counters are DELTAS,
 * not cumulative samples. Decoding does not authorize a flow or refresh it.
 * A single receiver must bind IDs to its current boot/session generation.
 * 1: decoded; 0: other message; -1: malformed matching statistics message.
 * Output is cleared on every rejection, including a truncated record array.
 */
int ffn_fe100_counters_decode(const uint8_t *, size_t, uint16_t trunk,
                             uint16_t return_port, struct ffn_fe100_counters *);
#endif
