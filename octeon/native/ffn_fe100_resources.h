/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef FFN_FE100_RESOURCES_H
#define FFN_FE100_RESOURCES_H
#include <stddef.h>
#include <stdint.h>
enum { FFN_RESOURCE_SMAC=1, FFN_RESOURCE_NEXTHOP=2 };
enum { FFN_RESOURCE_FETCH=1, FFN_RESOURCE_INSERT=2, FFN_RESOURCE_DELETE=3 };
/* One serialized table worker per process. The controller hash-verifies the
 * owner library through owner_fd before passing that same open descriptor.
 * No reset, initialization, implicit pool, or process-local recovery fallback.
 * Return negative errno on adapter failure, otherwise the native status (3 is
 * NOTFOUND for fetch only). After an uncertain operation only fetch is allowed.
 */
unsigned ffn_fe100_resources_abi(void);
int ffn_fe100_resources_open(int lock_fd, int owner_fd, uint64_t bar,
    unsigned kind, const uint32_t *pool, size_t count,
    const uint32_t *registers, size_t register_count, const char *trace);
int ffn_fe100_resources_call(unsigned operation, uint32_t index,
    uint8_t *data, size_t size);
#endif
