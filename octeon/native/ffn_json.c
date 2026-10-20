/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "ffn_json.h"
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct cursor { const char *start, *p, *end; char *err; size_t errsz; int depth; };

static void json_fail(struct cursor *c, const char *message)
{
    if (c->err && c->errsz) snprintf(c->err, c->errsz, "JSON: %s at offset %ld", message, (long)(c->p - c->start));
}

static struct ffn_json *node(enum ffn_json_type type)
{
    struct ffn_json *n = calloc(1, sizeof *n);
    if (n) n->type = type;
    return n;
}

struct ffn_json *ffn_json_null(void) { return node(FFN_JSON_NULL); }
struct ffn_json *ffn_json_bool(bool value) { struct ffn_json *n = node(FFN_JSON_BOOL); if (n) n->boolean = value; return n; }
struct ffn_json *ffn_json_int(long long value) { struct ffn_json *n = node(FFN_JSON_INT); if (n) n->integer = value; return n; }
struct ffn_json *ffn_json_number(double value) { struct ffn_json *n = node(FFN_JSON_NUMBER); if (n) n->number = value; return n; }
struct ffn_json *ffn_json_string(const char *value)
{
    struct ffn_json *n = node(FFN_JSON_STRING);
    if (!n) return NULL;
    n->string = strdup(value ? value : "");
    if (!n->string) { free(n); return NULL; }
    return n;
}
struct ffn_json *ffn_json_array(void) { return node(FFN_JSON_ARRAY); }
struct ffn_json *ffn_json_object(void) { return node(FFN_JSON_OBJECT); }

void ffn_json_free(struct ffn_json *n)
{
    if (!n) return;
    free(n->string);
    for (size_t i = 0; i < n->count; i++) {
        ffn_json_free(n->items[i]);
        if (n->keys) free(n->keys[i]);
    }
    free(n->items); free(n->keys); free(n);
}

static int grow(struct ffn_json *n, bool keyed)
{
    if (n->count < n->capacity) return 0;
    size_t cap = n->capacity ? n->capacity * 2 : 8;
    struct ffn_json **items = realloc(n->items, cap * sizeof *items);
    if (!items) return -1;
    n->items = items;
    if (keyed) {
        char **keys = realloc(n->keys, cap * sizeof *keys);
        if (!keys) return -1;
        n->keys = keys;
    }
    n->capacity = cap;
    return 0;
}

int ffn_json_push(struct ffn_json *array, struct ffn_json *value)
{
    if (!array || array->type != FFN_JSON_ARRAY || !value || grow(array, false)) { ffn_json_free(value); return -1; }
    array->items[array->count++] = value;
    return 0;
}

int ffn_json_set(struct ffn_json *object, const char *key, struct ffn_json *value)
{
    if (!object || object->type != FFN_JSON_OBJECT || !value) { ffn_json_free(value); return -1; }
    for (size_t i = 0; i < object->count; i++)
        if (strcmp(object->keys[i], key) == 0) { ffn_json_free(object->items[i]); object->items[i] = value; return 0; }
    if (grow(object, true)) { ffn_json_free(value); return -1; }
    char *copy = strdup(key);
    if (!copy) { ffn_json_free(value); return -1; }
    object->keys[object->count] = copy; object->items[object->count] = value; object->count++;
    return 0;
}

void ffn_json_remove(struct ffn_json *object, const char *key)
{
    if (!object || object->type != FFN_JSON_OBJECT) return;
    for (size_t i = 0; i < object->count; i++) {
        if (strcmp(object->keys[i], key) != 0) continue;
        ffn_json_free(object->items[i]); free(object->keys[i]);
        memmove(&object->items[i], &object->items[i + 1], (object->count - i - 1) * sizeof *object->items);
        memmove(&object->keys[i], &object->keys[i + 1], (object->count - i - 1) * sizeof *object->keys);
        object->count--;
        return;
    }
}

struct ffn_json *ffn_json_get(const struct ffn_json *object, const char *key)
{
    if (!object || object->type != FFN_JSON_OBJECT) return NULL;
    for (size_t i = 0; i < object->count; i++)
        if (strcmp(object->keys[i], key) == 0) return object->items[i];
    return NULL;
}

bool ffn_json_get_int(const struct ffn_json *object, const char *key, long long *out)
{
    const struct ffn_json *v = ffn_json_get(object, key);
    if (!v || v->type != FFN_JSON_INT) return false;
    *out = v->integer; return true;
}

bool ffn_json_get_bool(const struct ffn_json *object, const char *key, bool *out)
{
    const struct ffn_json *v = ffn_json_get(object, key);
    if (!v || v->type != FFN_JSON_BOOL) return false;
    *out = v->boolean; return true;
}

bool ffn_json_get_number(const struct ffn_json *o, const char *key, double *out)
{
    const struct ffn_json *v = ffn_json_get(o, key);
    if (!v) return false;
    if (v->type == FFN_JSON_INT) { *out = (double)v->integer; return true; }
    if (v->type == FFN_JSON_NUMBER) { *out = v->number; return true; }
    return false;
}

const char *ffn_json_get_string(const struct ffn_json *object, const char *key)
{
    const struct ffn_json *v = ffn_json_get(object, key);
    return v && v->type == FFN_JSON_STRING ? v->string : NULL;
}

struct ffn_json *ffn_json_copy(const struct ffn_json *n)
{
    if (!n) return NULL;
    struct ffn_json *c = NULL;
    switch (n->type) {
    case FFN_JSON_NULL: return ffn_json_null();
    case FFN_JSON_BOOL: return ffn_json_bool(n->boolean);
    case FFN_JSON_INT: return ffn_json_int(n->integer);
    case FFN_JSON_NUMBER: return ffn_json_number(n->number);
    case FFN_JSON_STRING: return ffn_json_string(n->string);
    case FFN_JSON_ARRAY:
        c = ffn_json_array();
        for (size_t i = 0; c && i < n->count; i++)
            if (ffn_json_push(c, ffn_json_copy(n->items[i]))) { ffn_json_free(c); return NULL; }
        return c;
    case FFN_JSON_OBJECT:
        c = ffn_json_object();
        for (size_t i = 0; c && i < n->count; i++)
            if (ffn_json_set(c, n->keys[i], ffn_json_copy(n->items[i]))) { ffn_json_free(c); return NULL; }
        return c;
    }
    return NULL;
}

bool ffn_json_equal(const struct ffn_json *a, const struct ffn_json *b)
{
    if (!a || !b || a->type != b->type) return false;
    switch (a->type) {
    case FFN_JSON_NULL: return true;
    case FFN_JSON_BOOL: return a->boolean == b->boolean;
    case FFN_JSON_INT: return a->integer == b->integer;
    case FFN_JSON_NUMBER: return a->number == b->number;
    case FFN_JSON_STRING: return strcmp(a->string, b->string) == 0;
    case FFN_JSON_ARRAY:
        if (a->count != b->count) return false;
        for (size_t i = 0; i < a->count; i++) if (!ffn_json_equal(a->items[i], b->items[i])) return false;
        return true;
    case FFN_JSON_OBJECT:
        if (a->count != b->count) return false;
        for (size_t i = 0; i < a->count; i++) {
            const struct ffn_json *other = ffn_json_get(b, a->keys[i]);
            if (!other || !ffn_json_equal(a->items[i], other)) return false;
        }
        return true;
    }
    return false;
}

/* ---- parser ---- */

static void skip(struct cursor *c) { while (c->p < c->end && (*c->p == ' ' || *c->p == '\t' || *c->p == '\n' || *c->p == '\r')) c->p++; }

static struct ffn_json *parse_value(struct cursor *c);

static int hex4(const char *s, unsigned *out)
{
    unsigned v = 0;
    for (int i = 0; i < 4; i++) {
        char ch = s[i]; v <<= 4;
        if (ch >= '0' && ch <= '9') v |= (unsigned)(ch - '0');
        else if (ch >= 'a' && ch <= 'f') v |= (unsigned)(ch - 'a' + 10);
        else if (ch >= 'A' && ch <= 'F') v |= (unsigned)(ch - 'A' + 10);
        else return -1;
    }
    *out = v; return 0;
}

static size_t put_utf8(char *out, unsigned cp)
{
    if (cp < 0x80) { out[0] = (char)cp; return 1; }
    if (cp < 0x800) { out[0] = (char)(0xc0 | (cp >> 6)); out[1] = (char)(0x80 | (cp & 0x3f)); return 2; }
    if (cp < 0x10000) { out[0] = (char)(0xe0 | (cp >> 12)); out[1] = (char)(0x80 | ((cp >> 6) & 0x3f)); out[2] = (char)(0x80 | (cp & 0x3f)); return 3; }
    out[0] = (char)(0xf0 | (cp >> 18)); out[1] = (char)(0x80 | ((cp >> 12) & 0x3f)); out[2] = (char)(0x80 | ((cp >> 6) & 0x3f)); out[3] = (char)(0x80 | (cp & 0x3f)); return 4;
}

static char *parse_string_raw(struct cursor *c)
{
    if (c->p >= c->end || *c->p != '"') { json_fail(c, "string expected"); return NULL; }
    c->p++;
    size_t cap = 32, len = 0;
    char *out = malloc(cap);
    if (!out) return NULL;
    while (c->p < c->end && *c->p != '"') {
        if (len + 8 >= cap) { char *n = realloc(out, cap *= 2); if (!n) { free(out); return NULL; } out = n; }
        unsigned char ch = (unsigned char)*c->p;
        if (ch < 0x20) { free(out); json_fail(c, "control character in string"); return NULL; }
        if (ch != '\\') { out[len++] = (char)ch; c->p++; continue; }
        c->p++;
        if (c->p >= c->end) break;
        char e = *c->p++;
        switch (e) {
        case '"': out[len++] = '"'; break;
        case '\\': out[len++] = '\\'; break;
        case '/': out[len++] = '/'; break;
        case 'b': out[len++] = '\b'; break;
        case 'f': out[len++] = '\f'; break;
        case 'n': out[len++] = '\n'; break;
        case 'r': out[len++] = '\r'; break;
        case 't': out[len++] = '\t'; break;
        case 'u': {
            unsigned cp;
            if (c->end - c->p < 4 || hex4(c->p, &cp)) { free(out); json_fail(c, "bad unicode escape"); return NULL; }
            c->p += 4;
            if (cp >= 0xd800 && cp <= 0xdbff && c->end - c->p >= 6 && c->p[0] == '\\' && c->p[1] == 'u') {
                unsigned lo;
                if (hex4(c->p + 2, &lo) == 0 && lo >= 0xdc00 && lo <= 0xdfff) { cp = 0x10000 + ((cp - 0xd800) << 10) + (lo - 0xdc00); c->p += 6; }
            }
            len += put_utf8(out + len, cp);
            break;
        }
        default: free(out); json_fail(c, "bad escape"); return NULL;
        }
    }
    if (c->p >= c->end) { free(out); json_fail(c, "unterminated string"); return NULL; }
    c->p++;
    out[len] = 0;
    return out;
}

static struct ffn_json *parse_number(struct cursor *c)
{
    const char *start = c->p;
    if (c->p < c->end && *c->p == '-') c->p++;
    if (c->p >= c->end || *c->p < '0' || *c->p > '9') { json_fail(c, "digit expected"); return NULL; }
    if (*c->p == '0') c->p++;
    else while (c->p < c->end && *c->p >= '0' && *c->p <= '9') c->p++;
    bool fraction = false;
    if (c->p < c->end && *c->p == '.') {
        fraction = true; c->p++;
        if (c->p >= c->end || *c->p < '0' || *c->p > '9') { json_fail(c, "digit expected"); return NULL; }
        while (c->p < c->end && *c->p >= '0' && *c->p <= '9') c->p++;
    }
    if (c->p < c->end && (*c->p == 'e' || *c->p == 'E')) {
        fraction = true; c->p++;
        if (c->p < c->end && (*c->p == '+' || *c->p == '-')) c->p++;
        if (c->p >= c->end || *c->p < '0' || *c->p > '9') { json_fail(c, "digit expected"); return NULL; }
        while (c->p < c->end && *c->p >= '0' && *c->p <= '9') c->p++;
    }
    errno = 0;
    char *stop;
    if (fraction) {
        char text[64]; size_t len = (size_t)(c->p - start);
        if (len >= sizeof text) { json_fail(c, "number too long"); return NULL; }
        memcpy(text, start, len); text[len] = 0;
        double d = strtod(text, &stop);
        if (errno || stop != text + len || !isfinite(d)) { json_fail(c, "number out of range"); return NULL; }
        return ffn_json_number(d);
    }
    long long v = strtoll(start, &stop, 10);
    if (errno || stop != c->p) { json_fail(c, "integer out of range"); return NULL; }
    return ffn_json_int(v);
}

static struct ffn_json *parse_value(struct cursor *c)
{
    skip(c);
    if (c->p >= c->end) { json_fail(c, "unexpected end"); return NULL; }
    if (++c->depth > 64) { json_fail(c, "nesting too deep"); return NULL; }
    struct ffn_json *result = NULL;
    switch (*c->p) {
    case '{': {
        c->p++;
        result = ffn_json_object();
        skip(c);
        if (c->p < c->end && *c->p == '}') { c->p++; break; }
        for (;;) {
            skip(c);
            char *key = parse_string_raw(c);
            if (!key) { ffn_json_free(result); result = NULL; break; }
            skip(c);
            if (c->p >= c->end || *c->p != ':') { free(key); ffn_json_free(result); result = NULL; json_fail(c, "colon expected"); break; }
            c->p++;
            struct ffn_json *value = parse_value(c);
            if (!value) { free(key); ffn_json_free(result); result = NULL; break; }
            int rc = ffn_json_set(result, key, value);
            free(key);
            if (rc) { ffn_json_free(result); result = NULL; break; }
            skip(c);
            if (c->p < c->end && *c->p == ',') { c->p++; continue; }
            if (c->p < c->end && *c->p == '}') { c->p++; break; }
            ffn_json_free(result); result = NULL; json_fail(c, "comma or brace expected"); break;
        }
        break;
    }
    case '[': {
        c->p++;
        result = ffn_json_array();
        skip(c);
        if (c->p < c->end && *c->p == ']') { c->p++; break; }
        for (;;) {
            struct ffn_json *value = parse_value(c);
            if (!value || ffn_json_push(result, value)) { ffn_json_free(result); result = NULL; break; }
            skip(c);
            if (c->p < c->end && *c->p == ',') { c->p++; continue; }
            if (c->p < c->end && *c->p == ']') { c->p++; break; }
            ffn_json_free(result); result = NULL; json_fail(c, "comma or bracket expected"); break;
        }
        break;
    }
    case '"': {
        char *s = parse_string_raw(c);
        if (s) { result = ffn_json_string(s); free(s); }
        break;
    }
    case 't': if (c->end - c->p >= 4 && !memcmp(c->p, "true", 4)) { c->p += 4; result = ffn_json_bool(true); } else json_fail(c, "bad literal"); break;
    case 'f': if (c->end - c->p >= 5 && !memcmp(c->p, "false", 5)) { c->p += 5; result = ffn_json_bool(false); } else json_fail(c, "bad literal"); break;
    case 'n': if (c->end - c->p >= 4 && !memcmp(c->p, "null", 4)) { c->p += 4; result = ffn_json_null(); } else json_fail(c, "bad literal"); break;
    default: result = parse_number(c); break;
    }
    c->depth--;
    return result;
}

struct ffn_json *ffn_json_parse(const char *text, size_t length, char *err, size_t errsz)
{
    struct cursor c = { text, text, text + length, err, errsz, 0 };
    if (err && errsz) err[0] = 0;
    struct ffn_json *v = parse_value(&c);
    if (!v) return NULL;
    skip(&c);
    if (c.p != c.end) { ffn_json_free(v); json_fail(&c, "trailing data"); return NULL; }
    return v;
}

/* ---- writer ---- */

struct buffer { char *data; size_t len, cap; bool failed; };

static void put(struct buffer *b, const char *s, size_t n)
{
    if (b->failed) return;
    if (b->len + n + 1 > b->cap) {
        size_t cap = b->cap ? b->cap : 256;
        while (cap < b->len + n + 1) cap *= 2;
        char *d = realloc(b->data, cap);
        if (!d) { b->failed = true; return; }
        b->data = d; b->cap = cap;
    }
    memcpy(b->data + b->len, s, n); b->len += n; b->data[b->len] = 0;
}

static void put_string(struct buffer *b, const char *s)
{
    put(b, "\"", 1);
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        char esc[8];
        switch (*p) {
        case '"': put(b, "\\\"", 2); break;
        case '\\': put(b, "\\\\", 2); break;
        case '\n': put(b, "\\n", 2); break;
        case '\r': put(b, "\\r", 2); break;
        case '\t': put(b, "\\t", 2); break;
        case '\b': put(b, "\\b", 2); break;
        case '\f': put(b, "\\f", 2); break;
        default:
            if (*p < 0x20) { snprintf(esc, sizeof esc, "\\u%04x", *p); put(b, esc, 6); }
            else put(b, (const char *)p, 1);
        }
    }
    put(b, "\"", 1);
}

/* Python's repr of a float: the shortest digits that read back as the same
 * double, in fixed notation while the exponent is in [-4, 16), else d.ddde+XX.
 * A monotonic clock in seconds or an epoch time must survive the trip ("%.6g"
 * lost whole seconds of either after a few days of uptime), and the owners'
 * hashes of configuration text must match what json.dumps produces. */
void ffn_json_number_text(double value, char *out, size_t outsz)
{
    char sci[48], digits[24], *p;
    int precision, exponent, n = 0;
    bool negative = value < 0 || (value == 0 && signbit(value));
    if (isnan(value) || isinf(value)) { snprintf(out, outsz, "%s", isnan(value) ? "NaN" : value > 0 ? "Infinity" : "-Infinity"); return; }
    for (precision = 0; precision <= 16; precision++) {
        snprintf(sci, sizeof sci, "%.*e", precision, value);
        if (strtod(sci, NULL) == value) break;
    }
    p = sci + (sci[0] == '-');
    for (; *p && *p != 'e'; p++) if (*p != '.') digits[n++] = *p;
    digits[n] = 0;
    exponent = atoi(p + 1);
    while (n > 1 && digits[n - 1] == '0') digits[--n] = 0;
    if (exponent >= -4 && exponent < 16) {
        char text[64]; int k = 0;
        if (negative) text[k++] = '-';
        if (exponent < 0) {
            text[k++] = '0'; text[k++] = '.';
            for (int i = 0; i < -exponent - 1; i++) text[k++] = '0';
            for (int i = 0; i < n; i++) text[k++] = digits[i];
        } else {
            for (int i = 0; i <= exponent; i++) text[k++] = i < n ? digits[i] : '0';
            text[k++] = '.';
            if (exponent + 1 < n) for (int i = exponent + 1; i < n; i++) text[k++] = digits[i];
            else text[k++] = '0';
        }
        text[k] = 0;
        snprintf(out, outsz, "%s", text);
    } else {
        snprintf(out, outsz, "%s%c%s%s%se%c%02d", negative ? "-" : "", digits[0], n > 1 ? "." : "", n > 1 ? digits + 1 : "", "",
                 exponent < 0 ? '-' : '+', exponent < 0 ? -exponent : exponent);
    }
}

static void dump(struct buffer *b, const struct ffn_json *n)
{
    char num[32];
    switch (n->type) {
    case FFN_JSON_NULL: put(b, "null", 4); break;
    case FFN_JSON_BOOL: if (n->boolean) put(b, "true", 4); else put(b, "false", 5); break;
    case FFN_JSON_INT: snprintf(num, sizeof num, "%lld", n->integer); put(b, num, strlen(num)); break;
    case FFN_JSON_NUMBER: ffn_json_number_text(n->number, num, sizeof num); put(b, num, strlen(num)); break;
    case FFN_JSON_STRING: put_string(b, n->string); break;
    case FFN_JSON_ARRAY:
        put(b, "[", 1);
        for (size_t i = 0; i < n->count; i++) { if (i) put(b, ", ", 2); dump(b, n->items[i]); }
        put(b, "]", 1); break;
    case FFN_JSON_OBJECT:
        put(b, "{", 1);
        for (size_t i = 0; i < n->count; i++) { if (i) put(b, ", ", 2); put_string(b, n->keys[i]); put(b, ": ", 2); dump(b, n->items[i]); }
        put(b, "}", 1); break;
    }
}

char *ffn_json_dump(const struct ffn_json *n)
{
    struct buffer b = { 0 };
    if (!n) return NULL;
    dump(&b, n);
    if (b.failed) { free(b.data); return NULL; }
    if (!b.data) return strdup("");
    return b.data;
}
