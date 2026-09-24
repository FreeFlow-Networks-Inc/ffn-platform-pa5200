/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Mainline GNU compiler adaptation for the OCTEON executive. */
#ifndef FFN_CVMX_COMPAT_H
#define FFN_CVMX_COMPAT_H

/*
 * CVMX_SHARED, WITHOUT CAVIUM'S COMPILER.
 *
 * cvmx-platform.h:116 defines it as `__attribute__ ((cvmx_shared))`, and
 * `cvmx_shared` is an attribute that exists ONLY in Cavium's patched gcc 4.7.
 * Mainline gcc does not know it and, per the standard, ignores an unrecognised
 * attribute with a warning -- so every CVMX_SHARED variable quietly becomes an
 * ordinary per-process one.
 *
 * THAT IS NOT A COSMETIC LOSS. cvmx_user_app_init() forks one process per core
 * and then spins:
 *
 *     while (cvmx_atomic_get32(&pending_fork))    [cvmx-app-init-linux.c:387]
 *
 * `pending_fork` is CVMX_SHARED. If it is not actually shared, each child
 * decrements its own copy, the parent's never reaches zero, and all 40
 * processes spin forever at 100% -- with no error message, because nothing
 * failed. Measured exactly that way before this define existed: 40 processes,
 * four minutes of CPU each, load average 25, and not one line of output.
 *
 * The fix is to make the attribute mean what Cavium's compiler made it mean.
 * The SDK's own linker script already gathers the section and brackets it with
 * the symbols the runtime reads:
 *
 *     __cvmx_shared_start = .;
 *     .cvmx_shared : { *(.cvmx_shared ...) }
 *     __cvmx_shared_end = .;
 *
 * so defining the ATTRIBUTE NAME as a section attribute is enough, and needs no
 * edit to any SDK header. It must be spelled this way round -- redefining
 * CVMX_SHARED itself cannot work from a force-included header, because
 * cvmx-platform.h defines it unconditionally afterwards and would win.
 *
 * Safe against collision: the only other appearances of the bare token in the
 * SDK are inside a string literal ("cvmx_shared" as an shm name) and a comment,
 * and macros expand in neither.
 *
 * VERIFY IT WORKED, because the failure is silent both ways:
 *     nm <binary> | grep cvmx_shared_    ->  start and end must DIFFER
 * Equal values mean the section is empty and the fork barrier will hang.
 */
#define cvmx_shared section(".cvmx_shared")

#endif /* FFN_CVMX_COMPAT_H */
