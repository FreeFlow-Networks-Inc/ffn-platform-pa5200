// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * ffn_dp_aho.c -- Aho-Corasick multi-pattern matching, software path.
 *
 * See ffn_dp_aho.h for why this is the primary path and not a fallback.
 *
 * REPRESENTATION
 * --------------
 * One flat table does double duty, which is the trick that keeps this short:
 *
 *   build    g[u*256 + c] is the trie's goto -- a child state, or 0 for none
 *   compile  the same cells are completed in place into a full DFA
 *
 * 0 can mean "no child" during the build because state 0 is the root and the
 * root is nobody's child. After compile() a 0 is a real transition (back to
 * the root), so the two readings never overlap in time.
 *
 * Match lists are a shared immutable chain. When BFS sets fail[v], the failure
 * state's chain is already complete -- that is what BFS order buys -- so v's
 * own list can simply be tacked onto the front of it rather than copied. The
 * chains are never mutated afterwards, so the sharing is safe and the whole
 * output structure stays O(number of patterns) instead of O(states x patterns).
 */

#include <stdlib.h>
#include <string.h>

#include "ffn_dp_aho.h"

#define ROOT        0u
#define OUT_NONE    0u          /* index 0 of out[] is a reserved sentinel */
#define ALPHABET    256u

struct aho_out {
    uint32_t id;
    uint32_t patlen;
    uint32_t next;              /* OUT_NONE terminates */
};

struct dp_aho {
    uint32_t       *g;          /* nstates * 256 transitions               */
    uint32_t       *fail;       /* nstates                                 */
    uint32_t       *out_first;  /* nstates, index into out[], OUT_NONE=end */
    uint32_t        nstates;
    uint32_t        cap_states;

    struct aho_out *out;
    uint32_t        nout;
    uint32_t        cap_out;

    uint32_t        npatterns;
    uint32_t        maxpatlen;
    int             case_fold;
    int             built;
    uint8_t         fold[ALPHABET];
};

/* ---- small helpers ---------------------------------------------------- */

static uint32_t *grow_u32(uint32_t *p, uint32_t oldn, uint32_t newn, uint32_t fill)
{
    uint32_t *q = realloc(p, (size_t)newn * sizeof(*q));
    if (!q)
        return NULL;
    for (uint32_t i = oldn; i < newn; i++)
        q[i] = fill;
    return q;
}

/* Grow the three per-state arrays together. They are always the same length;
 * keeping them in step here means no caller has to remember to. */
static int reserve_states(struct dp_aho *ac, uint32_t want)
{
    uint32_t cap, old;
    uint32_t *g, *f, *o;

    if (want <= ac->cap_states)
        return DP_AHO_OK;
    if (want > DP_AHO_MAX_STATES)
        return DP_AHO_TOO_MANY;

    cap = ac->cap_states ? ac->cap_states : 64u;
    while (cap < want) {
        cap *= 2u;
        if (cap > DP_AHO_MAX_STATES) {
            cap = DP_AHO_MAX_STATES;
            break;
        }
    }
    old = ac->cap_states;

    g = grow_u32(ac->g, old * ALPHABET, cap * ALPHABET, 0u);
    if (!g)
        return DP_AHO_NOMEM;
    ac->g = g;

    f = grow_u32(ac->fail, old, cap, ROOT);
    if (!f)
        return DP_AHO_NOMEM;
    ac->fail = f;

    o = grow_u32(ac->out_first, old, cap, OUT_NONE);
    if (!o)
        return DP_AHO_NOMEM;
    ac->out_first = o;

    ac->cap_states = cap;
    return DP_AHO_OK;
}

static int push_out(struct dp_aho *ac, uint32_t id, uint32_t patlen,
                    uint32_t next, uint32_t *idx_out)
{
    if (ac->nout + 1u > ac->cap_out) {
        uint32_t cap = ac->cap_out ? ac->cap_out * 2u : 32u;
        struct aho_out *p = realloc(ac->out, (size_t)cap * sizeof(*p));
        if (!p)
            return DP_AHO_NOMEM;
        ac->out = p;
        ac->cap_out = cap;
    }
    ac->out[ac->nout].id     = id;
    ac->out[ac->nout].patlen = patlen;
    ac->out[ac->nout].next   = next;
    *idx_out = ac->nout++;
    return DP_AHO_OK;
}

/* ---- build ------------------------------------------------------------ */

struct dp_aho *dp_aho_new(int case_mode)
{
    struct dp_aho *ac = calloc(1, sizeof(*ac));
    uint32_t dummy;

    if (!ac)
        return NULL;
    ac->case_fold = (case_mode == DP_AHO_CASE_FOLD);

    for (uint32_t i = 0; i < ALPHABET; i++)
        ac->fold[i] = (uint8_t)((ac->case_fold && i >= 'A' && i <= 'Z')
                                ? i + ('a' - 'A') : i);

    /* Root, plus the reserved out[0] so OUT_NONE==0 can mean "end". */
    if (reserve_states(ac, 1u) != DP_AHO_OK ||
        push_out(ac, 0u, 0u, OUT_NONE, &dummy) != DP_AHO_OK) {
        dp_aho_free(ac);
        return NULL;
    }
    ac->nstates = 1u;
    return ac;
}

int dp_aho_add(struct dp_aho *ac, const uint8_t *pat, uint32_t patlen,
               uint32_t id)
{
    uint32_t s = ROOT;
    uint32_t idx;
    int rc;

    if (!ac || !pat)
        return DP_AHO_BAD_ARG;
    if (ac->built)
        return DP_AHO_BAD_ARG;      /* adding after compile would lie */
    if (patlen == 0u || patlen > DP_AHO_MAX_PATLEN)
        return DP_AHO_BAD_ARG;

    for (uint32_t i = 0; i < patlen; i++) {
        uint8_t c = ac->fold[pat[i]];
        uint32_t nxt = ac->g[s * ALPHABET + c];

        if (nxt == ROOT) {          /* no child yet (root is nobody's child) */
            rc = reserve_states(ac, ac->nstates + 1u);
            if (rc != DP_AHO_OK)
                return rc;
            nxt = ac->nstates++;
            ac->g[s * ALPHABET + c] = nxt;
        }
        s = nxt;
    }

    /* Prepend; duplicate (id, pattern) pairs are the caller's business. */
    rc = push_out(ac, id, patlen, ac->out_first[s], &idx);
    if (rc != DP_AHO_OK)
        return rc;
    ac->out_first[s] = idx;

    ac->npatterns++;
    if (patlen > ac->maxpatlen)
        ac->maxpatlen = patlen;
    return DP_AHO_OK;
}

int dp_aho_compile(struct dp_aho *ac)
{
    uint32_t *queue;
    uint32_t head = 0, tail = 0;

    if (!ac)
        return DP_AHO_BAD_ARG;
    if (ac->built)
        return DP_AHO_OK;           /* idempotent */
    if (ac->npatterns == 0u)
        return DP_AHO_EMPTY;

    queue = malloc((size_t)ac->nstates * sizeof(*queue));
    if (!queue)
        return DP_AHO_NOMEM;

    /* Depth 1: every missing root transition stays at the root, and every
     * child of the root fails to the root. */
    for (uint32_t c = 0; c < ALPHABET; c++) {
        uint32_t v = ac->g[ROOT * ALPHABET + c];
        if (v != ROOT) {
            ac->fail[v] = ROOT;
            queue[tail++] = v;
        }
    }

    /* BFS. For each state u and byte c either there is a real child, in which
     * case its failure is where u's failure goes on c (already completed,
     * because BFS reaches fail[u] first), or there is not, in which case u's
     * transition simply becomes that same state. Completing the table here is
     * what removes the failure walk from the scan loop. */
    while (head < tail) {
        uint32_t u = queue[head++];

        for (uint32_t c = 0; c < ALPHABET; c++) {
            uint32_t v = ac->g[u * ALPHABET + c];
            uint32_t f = ac->g[ac->fail[u] * ALPHABET + c];

            if (v == ROOT) {
                ac->g[u * ALPHABET + c] = f;
                continue;
            }
            ac->fail[v] = f;
            queue[tail++] = v;
        }

        /* Splice this state's own matches in front of its failure state's
         * completed chain. Sharing the tail is safe: chains are immutable once
         * built, and every proper suffix match of u is exactly what fail[u]
         * already lists. */
        if (ac->out_first[ac->fail[u]] != OUT_NONE) {
            uint32_t m = ac->out_first[u];

            if (m == OUT_NONE) {
                ac->out_first[u] = ac->out_first[ac->fail[u]];
            } else {
                while (ac->out[m].next != OUT_NONE)
                    m = ac->out[m].next;
                ac->out[m].next = ac->out_first[ac->fail[u]];
            }
        }
    }

    free(queue);
    ac->built = 1;
    return DP_AHO_OK;
}

void dp_aho_free(struct dp_aho *ac)
{
    if (!ac)
        return;
    free(ac->g);
    free(ac->fail);
    free(ac->out_first);
    free(ac->out);
    free(ac);
}

/* ---- scan (datapath: no allocation, no mutation of ac) ---------------- */

int dp_aho_scan(const struct dp_aho *ac, uint32_t *state,
                const uint8_t *buf, size_t len,
                dp_aho_match_cb cb, void *ctx)
{
    const uint32_t *g;
    const uint8_t *fold;
    uint32_t s;
    int nmatch = 0;

    if (!ac || !state || (!buf && len))
        return DP_AHO_BAD_ARG;
    if (!ac->built)
        return DP_AHO_NOT_BUILT;
    if (*state >= ac->nstates)
        return DP_AHO_BAD_ARG;      /* caller handed us a foreign state */

    g = ac->g;
    fold = ac->fold;
    s = *state;

    for (size_t i = 0; i < len; i++) {
        uint32_t m;

        s = g[s * ALPHABET + fold[buf[i]]];

        for (m = ac->out_first[s]; m != OUT_NONE; m = ac->out[m].next) {
            nmatch++;
            if (cb && cb(ctx, ac->out[m].id, ac->out[m].patlen, i)) {
                *state = s;         /* consumed through this byte */
                return nmatch;
            }
        }
    }

    *state = s;
    return nmatch;
}

/* ---- introspection ---------------------------------------------------- */

uint32_t dp_aho_nstates(const struct dp_aho *ac)
{
    return ac ? ac->nstates : 0u;
}

uint32_t dp_aho_npatterns(const struct dp_aho *ac)
{
    return ac ? ac->npatterns : 0u;
}

size_t dp_aho_mem_use(const struct dp_aho *ac)
{
    if (!ac)
        return 0;
    return sizeof(*ac)
         + (size_t)ac->cap_states * ALPHABET * sizeof(uint32_t)
         + (size_t)ac->cap_states * 2u * sizeof(uint32_t)
         + (size_t)ac->cap_out * sizeof(struct aho_out);
}
