/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_l3_parse.h -- text -> address, shared by the v4 and v6 config layers.
 *
 * These were static helpers inside ffn_dp_l3_config.c until IPv6 needed the
 * same MAC, token and prefix handling. They are pure text conversion with no
 * dependency on either FIB, which is why they can live in one object that both
 * config layers link against without dragging the v6 tables into the v4 build.
 *
 * BYTE ORDER IS NOT UNIFORM HERE, AND THAT IS DELIBERATE.
 *
 *   v4 parses to HOST order, because struct dp_l3 stores host order. Using
 *   inet_pton() would give network order and require a swap back -- precisely
 *   the bug ffn_dp_oct.h warns about, which looks correct on x86 and corrupts
 *   every address on the big-endian OCTEON.
 *
 *   v6 parses to WIRE BYTES, because struct dp_l3_v6 stores wire bytes. A
 *   128-bit address has no meaningful host order to convert to, so there is
 *   nothing to swap and nothing to get wrong.
 *
 * Every parser REJECTS malformed input rather than saturating or truncating:
 * a typo must not silently become a route.
 */
#ifndef FFN_DP_L3_PARSE_H
#define FFN_DP_L3_PARSE_H

#include <stdint.h>

/* "10.1.2.3" -> host order. 0 on success, -1 on malformed input. */
int dp_l3_parse_ipv4(const char *s, uint32_t *out);

/* "10.1.0.0/16" -> prefix in host order + length 0..32. */
int dp_l3_parse_prefix(const char *s, uint32_t *ip, uint8_t *len);

/* "2001:db8::1" -> 16 wire bytes. Accepts "::" compression (at most one),
 * and an embedded dotted quad in the final position ("::ffff:192.0.2.1"). */
int dp_l3_parse_ipv6(const char *s, uint8_t out[16]);

/* "2001:db8::/32" -> 16 wire bytes + length 0..128. */
int dp_l3_parse_prefix6(const char *s, uint8_t out[16], uint8_t *len);

/* "02:aa:bb:cc:dd:ee" (or '-' separated) -> 6 wire bytes. */
int dp_l3_parse_mac(const char *s, uint8_t mac[6]);

/* Split into whitespace-separated tokens IN PLACE. Returns the token count. */
int dp_l3_tokenise(char *s, char **tok, int max);

/* "3" -> 3, rejecting anything that is not a bare non-negative number that
 * fits an egress id. The DP addresses interfaces by INDEX, never by name:
 * "dev enp11s0f0" is a syntax error here while looking perfectly well-formed
 * on the MP, so the renderer must resolve names before they get this far. */
int dp_l3_parse_dev(const char *s, uint16_t *out);

#endif /* FFN_DP_L3_PARSE_H */
