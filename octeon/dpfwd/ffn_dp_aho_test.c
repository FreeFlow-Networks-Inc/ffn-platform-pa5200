// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * ffn_dp_aho_test.c -- tests for the software Aho-Corasick path.
 *
 * Runs natively (x86-64) and under qemu-mips64 big-endian from the same
 * source, which is the point: the matcher is byte-oriented and must not
 * acquire an endianness by accident.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "ffn_dp_aho.h"

static int pass, fail;

static void chk(int cond, const char *what)
{
    if (cond) {
        pass++;
    } else {
        fail++;
        printf("  FAIL: %s\n", what);
    }
}

/* ---- match collection ------------------------------------------------- */

struct hit { uint32_t id; uint32_t patlen; size_t at; };

struct collector {
    struct hit h[64];
    int n;
    int stop_after;      /* 0 = never stop */
};

static int collect(void *ctx, uint32_t id, uint32_t patlen, size_t at)
{
    struct collector *c = ctx;

    if (c->n < (int)(sizeof(c->h) / sizeof(c->h[0]))) {
        c->h[c->n].id = id;
        c->h[c->n].patlen = patlen;
        c->h[c->n].at = at;
    }
    c->n++;
    return (c->stop_after && c->n >= c->stop_after) ? 1 : 0;
}

static int has_hit(const struct collector *c, uint32_t id, size_t at)
{
    for (int i = 0; i < c->n; i++)
        if (c->h[i].id == id && c->h[i].at == at)
            return 1;
    return 0;
}

/* ---- the canonical case ----------------------------------------------- */
/*
 * Patterns {he, she, his, hers} over "ushers" is the textbook Aho-Corasick
 * example, and it is here because it is the one that catches a broken output
 * chain: the state reached after "she" must ALSO report "he", which only
 * happens if failure-link outputs were spliced in correctly. A naive trie
 * reports "she" and silently misses "he".
 */
static void test_canonical(void)
{
    struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_SENSITIVE);
    struct collector c = { .n = 0 };
    int n;

    printf("canonical {he,she,his,hers} over \"ushers\"\n");
    chk(ac != NULL, "new");
    chk(dp_aho_add(ac, (const uint8_t *)"he",   2, 1) == DP_AHO_OK, "add he");
    chk(dp_aho_add(ac, (const uint8_t *)"she",  3, 2) == DP_AHO_OK, "add she");
    chk(dp_aho_add(ac, (const uint8_t *)"his",  3, 3) == DP_AHO_OK, "add his");
    chk(dp_aho_add(ac, (const uint8_t *)"hers", 4, 4) == DP_AHO_OK, "add hers");
    chk(dp_aho_compile(ac) == DP_AHO_OK, "compile");
    chk(dp_aho_npatterns(ac) == 4, "npatterns==4");

    uint32_t st = DP_AHO_START_STATE;
    n = dp_aho_scan(ac, &st, (const uint8_t *)"ushers", 6, collect, &c);

    chk(n == 3, "exactly 3 matches");
    chk(has_hit(&c, 2, 3), "she ends at 3");
    chk(has_hit(&c, 1, 3), "he ends at 3 (suffix via failure chain)");
    chk(has_hit(&c, 4, 5), "hers ends at 5");
    chk(!has_hit(&c, 3, 0) && !has_hit(&c, 3, 5), "his never matches");

    dp_aho_free(ac);
}

/* ---- streaming across segment boundaries ------------------------------ */
/*
 * The whole reason scan() takes a caller-held state. Split "ushers" every
 * possible way and the match set must not change. If it does, the automaton
 * is buffering something it should not.
 */
static void test_streaming(void)
{
    const char *text = "ushers";
    size_t len = 6;

    printf("streaming: every split of \"ushers\" gives the same 3 matches\n");

    for (size_t split = 0; split <= len; split++) {
        struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_SENSITIVE);
        struct collector c = { .n = 0 };
        uint32_t st = DP_AHO_START_STATE;
        int total = 0;
        char msg[64];

        dp_aho_add(ac, (const uint8_t *)"he",   2, 1);
        dp_aho_add(ac, (const uint8_t *)"she",  3, 2);
        dp_aho_add(ac, (const uint8_t *)"his",  3, 3);
        dp_aho_add(ac, (const uint8_t *)"hers", 4, 4);
        dp_aho_compile(ac);

        total += dp_aho_scan(ac, &st, (const uint8_t *)text, split, collect, &c);
        total += dp_aho_scan(ac, &st, (const uint8_t *)text + split,
                             len - split, collect, &c);

        snprintf(msg, sizeof(msg), "split at %zu -> 3 matches (got %d)",
                 split, total);
        chk(total == 3, msg);
        dp_aho_free(ac);
    }
}

/* ---- case folding ----------------------------------------------------- */
/*
 * Folding must apply to BOTH the pattern and the input. Folding one side only
 * is the classic way to get silent misses, so test a mixed-case pattern
 * against differently-mixed-case text.
 */
static void test_case_fold(void)
{
    struct dp_aho *cs = dp_aho_new(DP_AHO_CASE_SENSITIVE);
    struct dp_aho *cf = dp_aho_new(DP_AHO_CASE_FOLD);
    struct collector a = { .n = 0 }, b = { .n = 0 };
    uint32_t st;

    printf("case folding\n");
    dp_aho_add(cs, (const uint8_t *)"AbC", 3, 7);
    dp_aho_add(cf, (const uint8_t *)"AbC", 3, 7);
    dp_aho_compile(cs);
    dp_aho_compile(cf);

    st = DP_AHO_START_STATE;
    chk(dp_aho_scan(cs, &st, (const uint8_t *)"xxaBcxx", 7, collect, &a) == 0,
        "case-sensitive does NOT match aBc");

    st = DP_AHO_START_STATE;
    chk(dp_aho_scan(cf, &st, (const uint8_t *)"xxaBcxx", 7, collect, &b) == 1,
        "case-folding DOES match aBc");
    chk(has_hit(&b, 7, 4), "folded match ends at 4");

    dp_aho_free(cs);
    dp_aho_free(cf);
}

/* ---- overlapping and nested patterns ---------------------------------- */
static void test_overlap(void)
{
    struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_SENSITIVE);
    struct collector c = { .n = 0 };
    uint32_t st = DP_AHO_START_STATE;
    int n;

    printf("overlapping and nested patterns\n");
    dp_aho_add(ac, (const uint8_t *)"a",    1, 10);
    dp_aho_add(ac, (const uint8_t *)"aa",   2, 11);
    dp_aho_add(ac, (const uint8_t *)"aaa",  3, 12);
    dp_aho_compile(ac);

    /* "aaaa": a at 0,1,2,3 (4) ; aa at 1,2,3 (3) ; aaa at 2,3 (2) = 9 */
    n = dp_aho_scan(ac, &st, (const uint8_t *)"aaaa", 4, collect, &c);
    chk(n == 9, "nested a/aa/aaa over aaaa -> 9 matches");
    chk(has_hit(&c, 10, 0), "a at 0");
    chk(has_hit(&c, 11, 1), "aa at 1");
    chk(has_hit(&c, 12, 2), "aaa at 2");
    chk(has_hit(&c, 12, 3), "aaa at 3");

    dp_aho_free(ac);
}

/* ---- binary safety ---------------------------------------------------- */
/* Content is not text: NUL bytes and high bytes must match like any other. */
static void test_binary(void)
{
    struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_SENSITIVE);
    struct collector c = { .n = 0 };
    const uint8_t pat[4] = { 0x00, 0xff, 0x00, 0x80 };
    const uint8_t text[8] = { 0x11, 0x00, 0xff, 0x00, 0x80, 0x22, 0x00, 0xff };
    uint32_t st = DP_AHO_START_STATE;

    printf("binary patterns (NUL and high bytes)\n");
    chk(dp_aho_add(ac, pat, 4, 99) == DP_AHO_OK, "add binary pattern");
    dp_aho_compile(ac);
    chk(dp_aho_scan(ac, &st, text, 8, collect, &c) == 1, "one binary match");
    chk(has_hit(&c, 99, 4), "binary match ends at 4");

    dp_aho_free(ac);
}

/* ---- early exit ------------------------------------------------------- */
static void test_early_exit(void)
{
    struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_SENSITIVE);
    struct collector c = { .n = 0, .stop_after = 1 };
    uint32_t st = DP_AHO_START_STATE;
    int n;

    printf("callback early exit\n");
    dp_aho_add(ac, (const uint8_t *)"a", 1, 1);
    dp_aho_compile(ac);
    n = dp_aho_scan(ac, &st, (const uint8_t *)"aaaa", 4, collect, &c);
    chk(n == 1, "stopped after the first match");
    chk(st != DP_AHO_START_STATE, "state reflects bytes consumed");

    dp_aho_free(ac);
}

/* ---- argument and lifecycle errors ------------------------------------ */
static void test_errors(void)
{
    struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_SENSITIVE);
    uint32_t st = DP_AHO_START_STATE;
    uint8_t big[DP_AHO_MAX_PATLEN + 1];

    printf("error paths\n");
    memset(big, 'x', sizeof(big));

    chk(dp_aho_add(ac, (const uint8_t *)"", 0, 1) == DP_AHO_BAD_ARG,
        "empty pattern refused");
    chk(dp_aho_add(ac, big, DP_AHO_MAX_PATLEN + 1, 1) == DP_AHO_BAD_ARG,
        "over-long pattern refused");
    chk(dp_aho_add(ac, NULL, 4, 1) == DP_AHO_BAD_ARG, "NULL pattern refused");
    chk(dp_aho_scan(ac, &st, (const uint8_t *)"x", 1, NULL, NULL)
        == DP_AHO_NOT_BUILT, "scan before compile refused");
    chk(dp_aho_compile(ac) == DP_AHO_EMPTY, "compile with no patterns refused");

    chk(dp_aho_add(ac, (const uint8_t *)"ok", 2, 1) == DP_AHO_OK, "add ok");
    chk(dp_aho_compile(ac) == DP_AHO_OK, "compile ok");
    chk(dp_aho_compile(ac) == DP_AHO_OK, "compile is idempotent");
    chk(dp_aho_add(ac, (const uint8_t *)"late", 4, 2) == DP_AHO_BAD_ARG,
        "add after compile refused");

    st = 999999u;
    chk(dp_aho_scan(ac, &st, (const uint8_t *)"x", 1, NULL, NULL)
        == DP_AHO_BAD_ARG, "foreign state refused");

    /* A NULL automaton must not crash anything. */
    chk(dp_aho_nstates(NULL) == 0 && dp_aho_mem_use(NULL) == 0, "NULL safe");
    dp_aho_free(NULL);

    dp_aho_free(ac);
}

/* ---- a realistic-size set, to keep an eye on memory ------------------- */
static void test_scale(void)
{
    struct dp_aho *ac = dp_aho_new(DP_AHO_CASE_FOLD);
    char buf[32];
    size_t mem;

    printf("scale / memory\n");
    for (int i = 0; i < 2000; i++) {
        int n = snprintf(buf, sizeof(buf), "signature-%06d", i);
        if (dp_aho_add(ac, (const uint8_t *)buf, (uint32_t)n,
                       (uint32_t)i) != DP_AHO_OK) {
            chk(0, "add during scale test");
            dp_aho_free(ac);
            return;
        }
    }
    chk(dp_aho_compile(ac) == DP_AHO_OK, "compile 2000 patterns");
    mem = dp_aho_mem_use(ac);
    printf("    %u patterns -> %u states, %zu KiB (%.1f B/pattern-byte)\n",
           dp_aho_npatterns(ac), dp_aho_nstates(ac), mem / 1024,
           (double)mem / (2000.0 * 16.0));
    chk(dp_aho_nstates(ac) > 0 && mem > 0, "scale sane");

    /* and it still matches */
    struct collector c = { .n = 0 };
    uint32_t st = DP_AHO_START_STATE;
    chk(dp_aho_scan(ac, &st, (const uint8_t *)"xx SIGNATURE-001234 yy", 22,
                    collect, &c) == 1, "finds one of 2000, case-folded");
    chk(has_hit(&c, 1234, 18), "right pattern id at the right offset");

    dp_aho_free(ac);
}

int main(void)
{
    printf("=== ffn_dp_aho tests ===\n");
    test_canonical();
    test_streaming();
    test_case_fold();
    test_overlap();
    test_binary();
    test_early_exit();
    test_errors();
    test_scale();
    printf("=== %d passed, %d failed ===\n", pass, fail);
    return fail ? 1 : 0;
}
