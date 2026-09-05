/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_engine.h -- inline analysis engines for the dataplane.
 *
 * WHERE THIS SITS
 * ---------------
 * The control plane steers where L3 goes; the dataplane is where inline
 * analysis happens. ffn_dp_oct.c already produces the hook: a flow classified
 * FP_INSPECT_W is transmitted and flagged DP_FF_INSPECT, and its own comment
 * says "payload scan is a later stage". This is that stage.
 *
 *     dp_classify()  -> FP_INSPECT_W -> dp_engine_scan() -> verdict
 *                                            |
 *                            cached on the flow entry, so only the first
 *                            packets of a flow pay for analysis
 *
 * WHAT AN INLINE ENGINE MAY AND MAY NOT DO
 * ----------------------------------------
 * This runs per packet on a forwarding path, so the constraints are not style
 * preferences:
 *
 *   * NO allocation. Every engine works in caller-provided storage. A malloc on
 *     the packet path is a latency spike under exactly the traffic that matters.
 *   * BOUNDED work per packet. Each engine gets a byte budget
 *     (DP_ENGINE_SCAN_MAX) and must stop there. Unbounded scanning turns a
 *     jumbo frame into a denial of service against the forwarder itself.
 *   * NO backtracking regex. A pattern language with catastrophic backtracking
 *     is the same DoS wearing a nicer name. Matching here is literal or
 *     structural (digit runs, Luhn), and anything richer must be compiled to a
 *     bounded automaton before it reaches the dataplane.
 *   * BYTE ORIENTED. The OCTEON is big-endian MIPS64; every multi-byte read of
 *     packet data is a chance to be wrong on hardware while passing on x86. The
 *     engines here read bytes, and the tests run under qemu-mips64 for exactly
 *     this reason.
 *
 * VERDICTS
 * --------
 * An engine returns the strongest thing it found. The dispatcher combines them
 * so the most severe wins and scanning stops early on a block -- there is
 * nothing to learn from a second engine once the packet is going to be dropped.
 */
#ifndef FFN_DP_ENGINE_H
#define FFN_DP_ENGINE_H

#include <stdint.h>
#include <stddef.h>

/* Per-packet byte budget. 2 KB covers headers plus the start of a payload,
 * which is where protocol identification and most credential/PII leakage lives.
 * A larger budget buys little and costs it on every packet. */
#define DP_ENGINE_SCAN_MAX 2048

#define DP_ENGINE_NAME_MAX 24
#define DP_ENGINE_MAX      16

/* Engine verdicts, ordered by severity -- the dispatcher relies on the order,
 * so do not renumber without revisiting dp_engine_scan(). */
enum dp_engine_verdict {
	DP_EV_NONE  = 0,   /* nothing found                                  */
	DP_EV_ALERT = 1,   /* report it, let it pass                         */
	DP_EV_BLOCK = 2,   /* drop the packet and mark the flow              */
	DP_EV_RESET = 3    /* drop and tear the flow down                    */
};

/* Traffic direction, as the DLP rules express it. Deliberately explicit rather
 * than inferred inside an engine: "egress" means leaving the protected network,
 * which the forwarder knows from port roles and the engine does not. */
enum dp_direction {
	DP_DIR_UNKNOWN = 0,
	DP_DIR_INGRESS = 1,
	DP_DIR_EGRESS  = 2
};

/* What an engine is given. Everything is const or caller-owned: an engine that
 * mutated the packet would be a forwarding change disguised as analysis. */
struct dp_engine_ctx {
	const uint8_t *payload;     /* start of L4 payload, may be NULL       */
	uint32_t       payload_len; /* bytes available                        */
	uint8_t        direction;   /* enum dp_direction                      */
	uint8_t        l4_proto;    /* IPPROTO_TCP / UDP                      */
	uint16_t       dport;       /* host order, for protocol hints         */

	/* Filled in by the engine that fired, for logging. Never used to make a
	 * forwarding decision -- the verdict alone does that. */
	const char    *hit_engine;
	const char    *hit_rule;
	uint32_t       hit_offset;
};

/* One engine. `scan` returns a dp_engine_verdict and may set ctx->hit_*.
 * `enabled` is toggled from config (dp.engine.<name>.enable) rather than by
 * unregistering, so the stats of a disabled engine stay visible. */
struct dp_engine {
	char     name[DP_ENGINE_NAME_MAX];
	uint8_t  enabled;
	int    (*scan)(struct dp_engine_ctx *ctx, void *state);
	void    *state;

	uint64_t stat_scanned;
	uint64_t stat_alert;
	uint64_t stat_block;
};

struct dp_engine_set {
	struct dp_engine e[DP_ENGINE_MAX];
	uint32_t         count;
	uint64_t         stat_packets;   /* packets offered to the set        */
	uint64_t         stat_skipped;   /* no payload, or all engines off    */
};

/* Registry. Returns the engine index, or a negative DP_ERR_* on failure. */
int dp_engine_register(struct dp_engine_set *set, const char *name,
		       int (*scan)(struct dp_engine_ctx *, void *), void *state);

/* Enable/disable by name. Returns 0 if found, -1 otherwise, so a config key
 * naming an engine that does not exist is reported rather than ignored. */
int dp_engine_enable(struct dp_engine_set *set, const char *name, int on);

/* Run every enabled engine over one packet and return the combined verdict.
 * Stops at the first DP_EV_BLOCK or higher. */
int dp_engine_scan(struct dp_engine_set *set, struct dp_engine_ctx *ctx);

/* Config: dp.engine.<name>.enable = 0|1
 * Returns 1 if the key was consumed, 0 if it belongs to someone else. */
int dp_engine_config_line(struct dp_engine_set *set, const char *key,
			  const char *val);

#endif /* FFN_DP_ENGINE_H */
