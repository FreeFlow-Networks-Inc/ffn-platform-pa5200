/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_ctd.h -- content inspection engine: many signatures, one pass.
 *
 * WHAT THIS IS FOR
 * ----------------
 * ffn_dp_dlp.c matches its keyword rules one at a time, and its own comment
 * says what to do about that: "If keyword counts ever reach the dozens,
 * replace this with Aho-Corasick -- not with a regex." This is that engine.
 * It scans a packet ONCE for every signature at the same time, so adding the
 * thousandth pattern costs the same per byte as the first.
 *
 * It does not replace the DLP engine. DLP's credit-card, SSN and API-key rules
 * are structural (digit runs, Luhn), which is not something a literal
 * automaton can express. The two engines sit side by side and the dispatcher
 * combines their verdicts.
 *
 * WHY SOFTWARE
 * ------------
 * The PA-5220 has a hardware matcher, CA1. The appliance's own CP board-agent
 * log never mentions it across a whole service life -- the bring-up entry
 * points exist in the vendor's libfpga.so and are never called -- so content
 * inspection ran in software on this platform for real, in production, by the
 * vendor's own choice. See ffn_dp_aho.h.
 *
 * STREAMING
 * ---------
 * A signature split across two packets is still found, because the scanner's
 * position is carried on the flow (dp_engine_ctx.scan_state), one counter per
 * direction. Three limits are real and are enforced rather than hoped about:
 *
 *   * A clipped packet breaks the stream. dp_engine_scan() stops at
 *     DP_ENGINE_SCAN_MAX bytes, so the rest of that packet is never seen and
 *     the next packet is not contiguous with what was scanned. The state is
 *     reset to the root at that point. Losing a cross-packet match is correct;
 *     carrying state over a gap would report matches that are not in the
 *     traffic.
 *   * There is no TCP reassembly here. Bytes are scanned in arrival order, so
 *     a retransmit duplicates bytes and a reorder transposes them. Matches
 *     wholly inside one packet -- nearly all of them -- are unaffected; a
 *     cross-packet match can be missed, or in principle invented, by a
 *     reorder. Reassembly belongs in a flow layer, not in the matcher.
 *   * Scanning stops after DP_ENGINE_FLOW_PKTS payload packets of a flow.
 *     That is the forwarder's head-of-flow budget, not ours, and it bounds how
 *     long any of the above can matter.
 *
 * CONFIG
 * ------
 *   dp.ctd.case            = sensitive | fold      (before any signature)
 *   dp.ctd.sig.<name>      = <action>:<direction>:<pattern>
 *
 * action    alert | block | reset
 * direction any | ingress | egress
 * pattern   literal bytes; \xNN for a byte, \\ for a backslash. Nothing else
 *           is an escape -- an unknown one is rejected rather than passed
 *           through, because a signature that silently means something other
 *           than what was written is worse than one that fails to load.
 *
 * Signatures are staged as they arrive and only become live at
 * dp_ctd_commit(). Building the automaton on the packet path is not an option.
 */
#ifndef FFN_DP_CTD_H
#define FFN_DP_CTD_H

#include <stdint.h>

struct dp_engine_ctx;
struct dp_aho;

/* Fixed storage, like the DLP engine: the dataplane does not allocate on the
 * packet path, and a signature set that outgrows this must fail loudly at
 * config time rather than quietly drop rules. The automaton itself IS heap
 * allocated -- at build time, on the control plane, never during a scan. */
#define DP_CTD_SIG_MAX   256
#define DP_CTD_NAME_MAX  32

/* Return codes for the config/build side. The scan side returns a
 * dp_engine_verdict like every other engine. */
#define DP_CTD_OK          0
#define DP_CTD_FULL     (-70)
#define DP_CTD_BADARG   (-71)
#define DP_CTD_NOMEM    (-72)
#define DP_CTD_LOCKED   (-73)   /* add after commit                        */
#define DP_CTD_EMPTY    (-74)   /* commit with no signatures               */

struct dp_ctd_sig {
	char     name[DP_CTD_NAME_MAX];
	uint32_t patlen;
	uint8_t  action;        /* enum dp_engine_verdict                      */
	uint8_t  direction;     /* enum dp_direction                           */
	uint8_t  enabled;
	uint8_t  pad;
	uint64_t hits;
};

struct dp_ctd {
	struct dp_aho    *ac;
	struct dp_ctd_sig sig[DP_CTD_SIG_MAX];
	uint32_t          count;
	int               case_fold;
	int               built;

	/* Observability for the thing most likely to go wrong in the field: a
	 * signature set whose automaton is bigger than anyone expected. */
	uint64_t          stat_scanned;
	uint64_t          stat_matches;
	uint64_t          stat_streams_broken;   /* resets due to truncation   */
};

/* ---- build (control plane) -------------------------------------------- */

void dp_ctd_init(struct dp_ctd *d, int case_fold);

/* Stage one signature. `pat` is raw bytes, already unescaped. Returns the
 * signature index or a negative DP_CTD_*. */
int  dp_ctd_add(struct dp_ctd *d, const char *name,
		const uint8_t *pat, uint32_t patlen,
		int action, int direction);

/* Compile. Until this runs, dp_ctd_scan() finds nothing. */
int  dp_ctd_commit(struct dp_ctd *d);

/* Release the automaton and all signatures, so a new set can be loaded. */
void dp_ctd_reset(struct dp_ctd *d);

/* Bytes the compiled automaton occupies, for budgeting. 0 before commit. */
uint64_t dp_ctd_mem_use(const struct dp_ctd *d);

/* dp.ctd.* -- returns 1 consumed, 0 not ours, -1 consumed but malformed. */
int  dp_ctd_config_line(struct dp_ctd *d, const char *key, const char *val);

/* ---- scan (datapath) --------------------------------------------------- */

int  dp_ctd_scan(struct dp_engine_ctx *ctx, void *state);

#endif /* FFN_DP_CTD_H */
