/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Minimal JSON document model for the CP control binaries: strict RFC 8259
 * parsing of the shapes the MP and the BCM service exchange (objects, arrays,
 * strings, integers, booleans, null), and a canonical writer whose output the
 * Python callers read back with json.loads. A fractional or exponent number
 * becomes a NUMBER node (measurements such as optical power); integers stay
 * exact. */
#ifndef FFN_JSON_H
#define FFN_JSON_H
#include <stddef.h>
#include <stdbool.h>

enum ffn_json_type { FFN_JSON_NULL, FFN_JSON_BOOL, FFN_JSON_INT, FFN_JSON_NUMBER, FFN_JSON_STRING, FFN_JSON_ARRAY, FFN_JSON_OBJECT };

struct ffn_json {
    enum ffn_json_type type;
    bool boolean;
    long long integer;
    double number;                /* NUMBER: a measurement (fraction or exponent form); control messages never carry one */
    char *string;                 /* STRING */
    struct ffn_json **items;      /* ARRAY / OBJECT values */
    char **keys;                  /* OBJECT keys, parallel to items */
    size_t count, capacity;
};

struct ffn_json *ffn_json_parse(const char *text, size_t length, char *err, size_t errsz);
void ffn_json_free(struct ffn_json *node);

struct ffn_json *ffn_json_null(void);
struct ffn_json *ffn_json_bool(bool value);
struct ffn_json *ffn_json_int(long long value);
struct ffn_json *ffn_json_number(double value);
struct ffn_json *ffn_json_string(const char *value);
struct ffn_json *ffn_json_array(void);
struct ffn_json *ffn_json_object(void);
/* Takes ownership of value. Replaces an existing key in place. Returns 0 or -1 on allocation failure. */
int ffn_json_set(struct ffn_json *object, const char *key, struct ffn_json *value);
int ffn_json_push(struct ffn_json *array, struct ffn_json *value);
void ffn_json_remove(struct ffn_json *object, const char *key);

struct ffn_json *ffn_json_get(const struct ffn_json *object, const char *key);
/* Typed accessors: *out is written only when the key exists with that type; they return true then. */
bool ffn_json_get_int(const struct ffn_json *object, const char *key, long long *out);
bool ffn_json_get_bool(const struct ffn_json *object, const char *key, bool *out);
/* INT or NUMBER as a double. */
bool ffn_json_get_number(const struct ffn_json *object, const char *key, double *out);
const char *ffn_json_get_string(const struct ffn_json *object, const char *key);
struct ffn_json *ffn_json_copy(const struct ffn_json *node);
bool ffn_json_equal(const struct ffn_json *a, const struct ffn_json *b);

/* Serialise with sorted-insertion order preserved as inserted; returns a malloc'd string or NULL. */
char *ffn_json_dump(const struct ffn_json *node);

#endif
