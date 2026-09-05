/* SPDX-License-Identifier: GPL-2.0-or-later
 * ffn_dp_vsys_test.c -- the vsys hardware plan, checked without a chip.
 *
 * The plan is the half of hardware-enforced vsys that can be verified on a
 * workstation, and it is also the half most likely to be wrong: PCAM adds to a
 * style rather than setting it, so an ordering mistake here produces a chip
 * that quietly delivers one tenant's packets into another tenant's scheduling
 * group. That failure is invisible in a code review and expensive on hardware.
 */
#include "ffn_dp_vsys.h"
#include "ffn_dp_oct.h"

#include <stdio.h>
#include <string.h>

static int g_fail;
static void chk(int cond, const char *msg)
{
    printf("  %s %s\n", cond ? "ok  " : "FAIL", msg);
    if (!cond) g_fail++;
}

int main(void)
{
    struct dp_vsys_plan p;
    const uint8_t three[] = {1, 4, 7};
    int rc;

    printf("=== ffn_dp_vsys: hardware plan for virtual systems ===\n");
    printf("[host is %s-endian]\n",
           (*(const uint16_t *)"\x01\x02" == 0x0201) ? "little" : "big");

    /* ---------- 1. a plain plan ---------- */
    printf("\n[1] resource assignment\n");
    rc = dp_vsys_plan_build(&p, three, 3, &DP_VSYS_LIMITS_CN78XX, 8, 16, 4);
    chk(rc == DP_OK, "plan built");
    chk(p.count == 3, "three tenants planned");
    chk(p.res[0].vsys == 1 && p.res[0].sso_group == 8 &&
        p.res[0].qpg_offset == 16 && p.res[0].style == 4,
        "first tenant takes the base of each run");
    chk(p.res[2].vsys == 7 && p.res[2].sso_group == 10 &&
        p.res[2].qpg_offset == 18 && p.res[2].style == 6,
        "runs are contiguous, in request order");

    /* Distinctness is the point of the exercise: two tenants sharing a group
     * would share a scheduler, and sharing a QPG entry would share a buffer
     * aura -- either one silently removes the isolation this exists to give. */
    {
        int i, j, dup = 0;
        for (i = 0; i < (int)p.count; i++)
            for (j = i + 1; j < (int)p.count; j++)
                if (p.res[i].sso_group == p.res[j].sso_group ||
                    p.res[i].qpg_offset == p.res[j].qpg_offset ||
                    p.res[i].style == p.res[j].style)
                    dup++;
        chk(dup == 0, "no two tenants share a group, QPG entry or style");
    }

    /* ---------- 2. the receive path's lookup ---------- */
    printf("\n[2] SSO group -> vsys, which is the whole per-packet decision\n");
    chk(dp_vsys_of_group(&p, 8) == 1, "group 8 is vsys 1");
    chk(dp_vsys_of_group(&p, 10) == 7, "group 10 is vsys 7");
    chk(dp_vsys_of_group(&p, 9) == 4, "group 9 is vsys 4");
    chk(dp_vsys_of_group(&p, 0) == DP_VSYS_WILDCARD,
        "a group no tenant owns is the wildcard, not a tenant");
    chk(dp_vsys_of_group(&p, 200) == DP_VSYS_WILDCARD,
        "an unplanned group is the wildcard");
    chk(dp_vsys_of_group(&p, -1) == DP_VSYS_WILDCARD &&
        dp_vsys_of_group(&p, 100000) == DP_VSYS_WILDCARD,
        "out-of-range groups are rejected, not indexed");
    chk(dp_vsys_of_group(NULL, 8) == DP_VSYS_WILDCARD, "no plan is the wildcard");

    /* ---------- 3. PCAM adds, it does not set ---------- */
    printf("\n[3] PCAM style adder\n");
    chk(dp_vsys_pcam_style_add(&p, 1, 7) == 2,
        "moving vsys 1 -> 7 is an adder of 2");
    chk(dp_vsys_pcam_style_add(&p, 1, 1) == 0, "a move to itself adds nothing");
    chk(dp_vsys_pcam_style_add(&p, 7, 1) < 0,
        "a move DOWN is not expressible and must be refused, not clamped");
    chk(dp_vsys_pcam_style_add(&p, 1, 9) < 0, "an unplanned target is refused");
    chk(dp_vsys_pcam_style_add(&p, 9, 1) < 0, "an unplanned source is refused");

    /* ---------- 4. what the hardware cannot do ---------- */
    printf("\n[4] limits, refused rather than truncated\n");
    {
        uint8_t many[DP_VSYS_MAX + 1];
        unsigned i;
        for (i = 0; i < sizeof(many); i++) many[i] = (uint8_t)(i + 1);
        chk(dp_vsys_plan_build(&p, many, DP_VSYS_MAX + 1,
                               &DP_VSYS_LIMITS_CN78XX, 8, 16, 4) == DP_ERR_TOOMANY,
            "more tenants than the vsys space holds");
    }
    {
        /* A CN73XX has a quarter of the CN78XX's SSO groups. The same plan can
         * fit one part and not the other, which is exactly why the limits are a
         * parameter rather than a compile-time constant. */
        uint8_t ids[8];
        unsigned i;
        for (i = 0; i < 8; i++) ids[i] = (uint8_t)(i + 1);
        chk(dp_vsys_plan_build(&p, ids, 8, &DP_VSYS_LIMITS_CN73XX, 60, 16, 4)
            == DP_ERR_TOOMANY, "a run that overruns CN73XX's SSO groups");
        chk(dp_vsys_plan_build(&p, ids, 8, &DP_VSYS_LIMITS_CN78XX, 60, 16, 4)
            == DP_OK, "the same run fits on a CN78XX");
        chk(dp_vsys_plan_build(&p, ids, 8, &DP_VSYS_LIMITS_CN78XX, 8, 16, 60)
            == DP_ERR_TOOMANY, "a run that overruns the 64 final styles");
    }
    {
        const uint8_t withzero[] = {1, 0, 2};
        const uint8_t dupd[]     = {1, 2, 1};
        const uint8_t toobig[]   = {1, DP_VSYS_MAX + 1};
        chk(dp_vsys_plan_build(&p, withzero, 3, &DP_VSYS_LIMITS_CN78XX, 8, 16, 4)
            == DP_ERR_RANGE,
            "vsys 0 is dp_classify's wildcard and gets no hardware");
        chk(dp_vsys_plan_build(&p, dupd, 3, &DP_VSYS_LIMITS_CN78XX, 8, 16, 4)
            == DP_ERR_RANGE, "a tenant cannot own two groups");
        chk(dp_vsys_plan_build(&p, toobig, 2, &DP_VSYS_LIMITS_CN78XX, 8, 16, 4)
            == DP_ERR_RANGE, "a vsys id above the supported range");
        chk(dp_vsys_plan_build(&p, three, 3, &DP_VSYS_LIMITS_CN78XX, 0, 16, 4)
            == DP_ERR_RANGE, "group 0 belongs to the SDK's default path");
        chk(dp_vsys_plan_build(&p, three, 3, &DP_VSYS_LIMITS_CN78XX, 8, 0, 4)
            == DP_ERR_RANGE, "QPG 0 belongs to the SDK's default path");
        chk(dp_vsys_plan_build(&p, three, 3, &DP_VSYS_LIMITS_CN78XX, 8, 16, 0)
            == DP_ERR_RANGE, "style 0 belongs to the SDK's default path");
    }

    /* ---------- 5. an empty plan is valid and inert ---------- */
    printf("\n[5] no tenants\n");
    rc = dp_vsys_plan_build(&p, NULL, 0, &DP_VSYS_LIMITS_CN78XX, 8, 16, 4);
    chk(rc == DP_OK && p.count == 0, "a box with no tenants plans nothing");
    chk(dp_vsys_of_group(&p, 8) == DP_VSYS_WILDCARD,
        "and every group is then the wildcard, which is single-vsys behaviour");

    printf("\n==== ffn_dp_vsys test: %d failed ====\n", g_fail);
    return g_fail ? 1 : 0;
}
