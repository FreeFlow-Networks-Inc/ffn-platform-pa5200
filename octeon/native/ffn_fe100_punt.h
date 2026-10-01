/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef FFN_FE100_PUNT_H
#define FFN_FE100_PUNT_H
#include <stddef.h>
#include <stdint.h>
struct ffn_fe100_punt_scope {
    uint16_t trunk, return_port, front, in_lif, zone;
};
struct ffn_fe100_punt {
    const uint8_t *frame;
    size_t length;
    uint32_t flow_id;
    uint16_t front, in_lif, zone;
    uint8_t message, code;
};
/* Only physically qualified, original-packet formats are accepted. Return 1
 * for a software-path packet, 0 for an unsupported/malformed envelope.
 * Scope must come from the attachment owner, never packet-derived learning.
 */
int ffn_fe100_punt_decode(const uint8_t *,size_t,
    const struct ffn_fe100_punt_scope *,struct ffn_fe100_punt *);
#endif
