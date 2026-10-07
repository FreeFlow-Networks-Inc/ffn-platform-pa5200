/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_aho.h -- multi-pattern content matching for the dataplane.
 *
 * Aho-Corasick, in software. The algorithm is Aho & Corasick 1975 and needs no
 * reversing; this is FFN's own implementation of it, matching FFN's own
 * patterns.
 *
 * WHY SOFTWARE FIRST
 * ------------------
 * The PA-5220 has a hardware Aho-Corasick engine (CA1, PCI feed:a101) sitting
 * behind the dataplane Octeon's Interlaken link. On our unit that link never
 * came up -- the vendor's own DP board agent logged "ILK enable failed: -1" on
 * its first boot in Feb 2025 and every boot after -- so CA1 was never usable.
 * The vendor ships a software path beside the hardware one for exactly this
 * case, and the appliance ran its whole service life on it without complaint.
 * So software is not a fallback here, it is THE path, and the hardware is an
 * optimisation we may never be able to switch on.
 *
 * SHAPE
 * -----
 * Two phases, deliberately separated:
 *
 *   build   control plane, allocates, slow, done once per pattern set
 *   scan    datapath, ZERO allocation, re-entrant, O(1) per byte
 *
 * The scan side never allocates and never mutates the automaton, so one
 * compiled dp_aho may be shared read-only by every core. Per-flow progress
 * lives in a caller-held uint32_t, which is what makes matching across packet
 * boundaries work: a pattern split over two segments of a stream is found
 * because the caller carries the state, not because we buffer anything.
 *
 * MEMORY
 * ------
 * The scan table is a full 256-way transition table, so matching costs one
 * indexed load per byte with no failure walk. That trade is deliberate: a
 * goto+failure automaton is perhaps 8x smaller but has an unbounded failure
 * walk per byte, and an unbounded per-byte cost in a datapath is a latency
 * cliff under adversarial input -- which is precisely the input this will see.
 * Cost is 1 KiB per state; call dp_aho_mem_use() and budget for it.
 */
#ifndef FFN_DP_AHO_H
#define FFN_DP_AHO_H

#include <stdint.h>
#include <stddef.h>

/* Return codes. Negative is failure, as elsewhere in the forwarder. */
#define DP_AHO_OK            0
#define DP_AHO_NOMEM      (-60)
#define DP_AHO_TOO_MANY   (-61)   /* more states than DP_AHO_MAX_STATES     */
#define DP_AHO_BAD_ARG    (-62)
#define DP_AHO_NOT_BUILT  (-63)   /* scan before dp_aho_compile()           */
#define DP_AHO_EMPTY      (-64)   /* compile with no patterns               */

/* A pattern longer than this is refused at add time. Nothing in content
 * matching needs more, and the cap keeps one bad rule from exploding the
 * state count. */
#define DP_AHO_MAX_PATLEN   1024u

/* Hard ceiling on states. At 1 KiB of transition table each this is a 64 MiB
 * automaton, which is already far more than a sane rule set needs; hitting it
 * means the pattern set is wrong, not that the limit is too low. */
#define DP_AHO_MAX_STATES   65536u

/* Case folding, chosen at compile time and then fixed. Folding is applied to
 * the patterns when they are added AND to the input as it is scanned, so the
 * two cannot disagree -- the classic way to get silent misses is to fold one
 * side only. ASCII only: folding UTF-8 needs normalisation, which does not
 * belong in a per-byte datapath loop. */
#define DP_AHO_CASE_SENSITIVE  0
#define DP_AHO_CASE_FOLD       1

struct dp_aho;

/* Reported once per (pattern, end position). `at` is the offset of the LAST
 * byte of the match within the buffer just passed to dp_aho_scan().
 *
 * `at` can be less than patlen-1, and start can therefore be negative: that
 * means the match began in an earlier segment of the stream. That is not an
 * error, it is the streaming case working. Compute the absolute position from
 * a running byte count the caller keeps; this module deliberately holds no
 * stream offset of its own, because then there is no counter to get out of
 * step with the caller's.
 *
 * Return non-zero to stop the scan early (first-match-wins policies); the
 * scan returns the number of matches reported so far either way.
 */
typedef int (*dp_aho_match_cb)(void *ctx, uint32_t pattern_id,
                               uint32_t patlen, size_t at);

/* ---- build phase (control plane) ------------------------------------- */

struct dp_aho *dp_aho_new(int case_mode);

/* Add one pattern. id is the caller's, returned verbatim on match; it need not
 * be unique, so several rules may share an id. Copies the bytes. */
int dp_aho_add(struct dp_aho *ac, const uint8_t *pat, uint32_t patlen,
               uint32_t id);

/* Build the failure links and the transition table. Must be called once, after
 * all patterns are added and before any scan. */
int dp_aho_compile(struct dp_aho *ac);

void dp_aho_free(struct dp_aho *ac);

/* ---- scan phase (datapath, no allocation) ----------------------------- */

#define DP_AHO_START_STATE  0u

/* Scan one segment. *state carries progress between calls and must start at
 * DP_AHO_START_STATE for a new stream. Returns the number of matches reported,
 * or a negative DP_AHO_* code. cb may be NULL to count without reporting. */
int dp_aho_scan(const struct dp_aho *ac, uint32_t *state,
                const uint8_t *buf, size_t len,
                dp_aho_match_cb cb, void *ctx);

/* ---- introspection ---------------------------------------------------- */

uint32_t dp_aho_nstates(const struct dp_aho *ac);
uint32_t dp_aho_npatterns(const struct dp_aho *ac);
size_t   dp_aho_mem_use(const struct dp_aho *ac);

#endif /* FFN_DP_AHO_H */
