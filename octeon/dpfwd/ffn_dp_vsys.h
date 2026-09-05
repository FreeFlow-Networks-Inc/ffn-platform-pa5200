/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_vsys.h -- virtual systems, enforced by the packet hardware.
 *
 * WHY THIS IS NOT A SOFTWARE TAG
 * ------------------------------
 * The forwarder has always carried a vsys byte: dp_tuple.vsys, dp_flow_key.vsys
 * and dp_policy_row.vsys are real, the flow cache is keyed on vsys so two
 * tenants cannot alias one 5-tuple, and dp_classify treats 0 as a wildcard.
 * What was missing is where that byte comes from. It came from a single
 * command-line `-v N` applied to every port, which makes multi-tenancy a
 * labelling exercise: create a second vsys, assign a port, and packets still
 * arrive tagged 1.
 *
 * A software fix -- a port->vsys array consulted per packet -- would work and
 * would be wrong. It puts a lookup on the packet path for something the chip
 * can decide for free, and it gives no isolation: one tenant's traffic can
 * still starve another's, because they share one scheduling group and one
 * buffer pool.
 *
 * OCTEON III already partitions all three. The chain is:
 *
 *      port (pkind) --> PKI style --> QPG entry --> { SSO group, FPA aura }
 *
 * so one style and one QPG entry per vsys gives each tenant its own scheduling
 * group AND its own buffer accounting, decided in hardware at wire speed. The
 * forwarder then reads the vsys back out of the work queue entry's group field
 * -- no table, no branch, no per-packet cost.
 *
 * PCAM is how a vsys is chosen by something finer than the port: match a VLAN
 * id, an ethertype, a destination prefix, and land on a different style.
 *
 * THE CONSTRAINT THAT SHAPES THIS FILE
 * ------------------------------------
 * A PCAM entry's action is `style_add` -- it ADDS to the interim style, it does
 * not set it (cvmx-pki.h: "Resulting interim style adder"). So the styles a
 * given port can reach must be laid out at ASCENDING offsets from that port's
 * base style, and the adder is a difference, never an index. A design that
 * assigns styles arbitrarily and then expects PCAM to select one cannot work,
 * and would fail on hardware in a way that is invisible in a code review.
 *
 * That is the whole reason the plan is built and validated here, in portable
 * code, instead of being computed inline while programming registers: this is
 * the part that is easy to get wrong and possible to test without a chip.
 *
 * NOTHING HERE TOUCHES HARDWARE. It computes and checks a plan. The CVMX calls
 * that apply it live in ffn_dp_io_octeon3.c, which is compiled against the real
 * SDK by `make oct3-cvmx`.
 */
#ifndef FFN_DP_VSYS_H
#define FFN_DP_VSYS_H

#include <stdint.h>

/* vsys 0 is the wildcard in dp_classify(), so it is not a tenant and never gets
 * hardware resources. Tenants are 1..DP_VSYS_MAX. */
#define DP_VSYS_MAX        32u
#define DP_VSYS_WILDCARD    0u

/* Hardware limits, passed in rather than compiled in so the planner can be
 * exercised for a part that is not the one being built for -- a CN73XX has a
 * quarter of the CN78XX's SSO groups, and a plan that fits one may not fit the
 * other. Values come from the SDK at runtime: CVMX_PKI_NUM_SSO_GROUP is
 * cvmx_sso_num_xgrp() (256 on CN78XX, 64 on CN73XX), CVMX_PKI_NUM_QPG_ENTRY is
 * 2048, CVMX_PKI_NUM_FINAL_STYLE is 64 and CVMX_PKI_NUM_INTERNAL_STYLE is 256.
 */
struct dp_vsys_limits {
    uint16_t sso_groups;      /* CVMX_PKI_NUM_SSO_GROUP        */
    uint16_t qpg_entries;     /* CVMX_PKI_NUM_QPG_ENTRY        */
    uint16_t final_styles;    /* CVMX_PKI_NUM_FINAL_STYLE      */
    uint16_t internal_styles; /* CVMX_PKI_NUM_INTERNAL_STYLE   */
};

/* The CN78XX the dataplane runs on. Provided as a constant so a caller with no
 * SDK -- the test harness, or a build without FFN_HAVE_CVMX -- can still plan. */
extern const struct dp_vsys_limits DP_VSYS_LIMITS_CN78XX;
extern const struct dp_vsys_limits DP_VSYS_LIMITS_CN73XX;

struct dp_vsys_res {
    uint8_t  vsys;        /* 1..DP_VSYS_MAX                              */
    uint16_t sso_group;   /* SSO group this tenant's work is scheduled in */
    uint16_t qpg_offset;  /* index into PKI_QPG_TBL                       */
    uint8_t  style;       /* PKI final style pointing at that QPG entry   */
};

/* Reverse lookup for the receive path: SSO group -> vsys. Sized by the largest
 * group count any supported part has, so the table is a plain array index and
 * the packet path needs no search. 0 means "no tenant owns this group", which
 * is the wildcard and the correct answer for work that did not come from a
 * tenant style. */
#define DP_VSYS_GROUPS_MAX 256u

struct dp_vsys_plan {
    struct dp_vsys_res res[DP_VSYS_MAX];
    uint32_t count;

    /* Where the contiguous runs start. Contiguity is not tidiness: PCAM can
     * only ADD to a style, so reaching another tenant's style from a port's
     * base style requires a known non-negative difference. */
    uint16_t sso_group_base;
    uint16_t qpg_base;
    uint8_t  style_base;

    uint8_t  group_to_vsys[DP_VSYS_GROUPS_MAX];
    struct dp_vsys_limits lim;
};

/* Build a plan for `n` tenant vsys ids.
 *
 * Returns DP_OK, or a negative DP_ERR_*:
 *   DP_ERR_RANGE   an id is 0 (the wildcard) or above DP_VSYS_MAX, or a
 *                  duplicate, or the requested bases do not fit the part.
 *   DP_ERR_TOOMANY more tenants than the hardware can give distinct groups,
 *                  QPG entries or final styles.
 *
 * `group_base`, `qpg_base` and `style_base` are where the runs start. Group 0,
 * QPG 0 and style 0 are left to the SDK's own bring-up, which uses them for the
 * default path; a plan that starts at 0 would quietly take them over.
 */
int dp_vsys_plan_build(struct dp_vsys_plan *p,
                       const uint8_t *vsys_ids, uint32_t n,
                       const struct dp_vsys_limits *lim,
                       uint16_t group_base, uint16_t qpg_base,
                       uint8_t style_base);

/* The tenant that owns an SSO group, or DP_VSYS_WILDCARD.
 * This is the receive path's whole vsys decision, so it is a bounds check and
 * an array index and nothing else. */
static inline uint8_t dp_vsys_of_group(const struct dp_vsys_plan *p, int grp)
{
    if (!p || grp < 0 || (unsigned)grp >= DP_VSYS_GROUPS_MAX)
        return DP_VSYS_WILDCARD;
    return p->group_to_vsys[grp];
}

/* The resources planned for one tenant, or NULL. */
const struct dp_vsys_res *dp_vsys_find(const struct dp_vsys_plan *p,
                                       uint8_t vsys);

/* The value a PCAM entry's action.style_add must carry to move a packet from
 * `from_vsys`'s style to `to_vsys`'s.
 *
 * Returns a negative DP_ERR_* when the move is impossible, and callers MUST
 * check: PCAM adds, so moving to a lower style cannot be expressed, and the sum
 * must stay inside the interim style space. Silently clamping would produce a
 * PCAM entry that matches and sends the packet to the wrong tenant.
 */
int dp_vsys_pcam_style_add(const struct dp_vsys_plan *p,
                           uint8_t from_vsys, uint8_t to_vsys);

#endif /* FFN_DP_VSYS_H */
