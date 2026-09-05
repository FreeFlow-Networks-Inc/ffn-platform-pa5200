/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * ffn_dp_vsys.c -- plan the hardware resources a set of virtual systems needs.
 *
 * Pure computation: no CVMX, no registers, no allocation. See the header for
 * why the plan is built separately from the code that programs it.
 */
#include <string.h>

#include "ffn_dp_vsys.h"
#include "ffn_dp_oct.h"          /* DP_OK / DP_ERR_* */

/* From the SDK, for the parts this dataplane runs on:
 *   CVMX_PKI_NUM_SSO_GROUP      cvmx_sso_num_xgrp(): 256 CN78XX, 64 CN73XX
 *   CVMX_PKI_NUM_QPG_ENTRY      2048
 *   CVMX_PKI_NUM_FINAL_STYLE    64
 *   CVMX_PKI_NUM_INTERNAL_STYLE 256
 * Duplicated here rather than included, because this file must build with no
 * SDK present; ffn_dp_io_octeon3.c asserts these against the real constants
 * when it is compiled against the SDK, so a divergence is caught there.
 */
const struct dp_vsys_limits DP_VSYS_LIMITS_CN78XX = {
    .sso_groups = 256, .qpg_entries = 2048,
    .final_styles = 64, .internal_styles = 256,
};
const struct dp_vsys_limits DP_VSYS_LIMITS_CN73XX = {
    .sso_groups = 64,  .qpg_entries = 2048,
    .final_styles = 64, .internal_styles = 256,
};

int dp_vsys_plan_build(struct dp_vsys_plan *p,
                       const uint8_t *vsys_ids, uint32_t n,
                       const struct dp_vsys_limits *lim,
                       uint16_t group_base, uint16_t qpg_base,
                       uint8_t style_base)
{
    uint32_t i, j;

    if (!p || !lim || (n && !vsys_ids))
        return DP_ERR_RANGE;
    if (n > DP_VSYS_MAX)
        return DP_ERR_TOOMANY;

    memset(p, 0, sizeof(*p));
    p->lim = *lim;
    p->sso_group_base = group_base;
    p->qpg_base = qpg_base;
    p->style_base = style_base;

    /* Refuse a plan that cannot be represented, rather than one that overlaps
     * the SDK's own default resources or runs off the end of a table. Each of
     * these has a different cause and a different fix, so they are checked
     * separately instead of as one "does it fit". */
    if (group_base == 0 || qpg_base == 0 || style_base == 0)
        return DP_ERR_RANGE;          /* index 0 belongs to the SDK's default path */
    if ((uint32_t)group_base + n > lim->sso_groups)
        return DP_ERR_TOOMANY;
    if ((uint32_t)qpg_base + n > lim->qpg_entries)
        return DP_ERR_TOOMANY;
    if ((uint32_t)style_base + n > lim->final_styles)
        return DP_ERR_TOOMANY;
    /* The reverse map is indexed by group, so every group this plan hands out
     * has to be addressable in it. A part with more groups than the table is a
     * build-time mistake, not a runtime one, but it would corrupt memory. */
    if ((uint32_t)group_base + n > DP_VSYS_GROUPS_MAX)
        return DP_ERR_TOOMANY;

    for (i = 0; i < n; i++) {
        uint8_t v = vsys_ids[i];

        /* 0 is dp_classify()'s wildcard: it matches every tenant's traffic, so
         * giving it hardware resources would create a tenant whose rules apply
         * to everyone. Rejected rather than skipped, because a caller asking
         * for it has misunderstood something worth telling them about. */
        if (v == DP_VSYS_WILDCARD || v > DP_VSYS_MAX)
            return DP_ERR_RANGE;
        for (j = 0; j < i; j++)
            if (vsys_ids[j] == v)
                return DP_ERR_RANGE;   /* a tenant cannot own two groups */

        p->res[i].vsys       = v;
        p->res[i].sso_group  = (uint16_t)(group_base + i);
        p->res[i].qpg_offset = (uint16_t)(qpg_base + i);
        p->res[i].style      = (uint8_t)(style_base + i);
        p->group_to_vsys[group_base + i] = v;
    }
    p->count = n;
    return DP_OK;
}

const struct dp_vsys_res *dp_vsys_find(const struct dp_vsys_plan *p, uint8_t vsys)
{
    uint32_t i;

    if (!p)
        return NULL;
    for (i = 0; i < p->count; i++)
        if (p->res[i].vsys == vsys)
            return &p->res[i];
    return NULL;
}

int dp_vsys_pcam_style_add(const struct dp_vsys_plan *p,
                           uint8_t from_vsys, uint8_t to_vsys)
{
    const struct dp_vsys_res *from = dp_vsys_find(p, from_vsys);
    const struct dp_vsys_res *to   = dp_vsys_find(p, to_vsys);
    int add;

    if (!from || !to)
        return DP_ERR_RANGE;

    /* PCAM ADDS. A move to a lower style is not expressible, and the caller has
     * to reorder its styles or pick a different base port -- which is exactly
     * the kind of thing that must fail loudly at plan time, because on hardware
     * it would silently deliver a tenant's packets to another tenant's group.
     */
    if (to->style < from->style)
        return DP_ERR_RANGE;

    add = (int)to->style - (int)from->style;

    /* The adder is an 8-bit field, and the result is an INTERIM style, which
     * lives in the larger internal style space before being mapped down to a
     * final style. Both bounds are real. */
    if (add > 255)
        return DP_ERR_RANGE;
    if ((uint32_t)from->style + (uint32_t)add >= p->lim.internal_styles)
        return DP_ERR_RANGE;
    return add;
}
