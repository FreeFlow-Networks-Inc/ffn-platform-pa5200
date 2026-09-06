/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * execinfo.h -- musl compatibility stub.
 *
 * The SDK's cvmx-interrupt.c includes <execinfo.h> to print a backtrace when it
 * catches a fatal exception. That header and its backtrace()/backtrace_symbols()
 * are a glibc extension; musl has no equivalent, by design.
 *
 * cvmx-interrupt.c is not optional -- cvmx_interrupt_register(),
 * cvmx_interrupt_map() and cvmx_interrupt_in_isr() are referenced from the
 * packet path -- so the file has to compile.
 *
 * THESE STUBS DEGRADE DIAGNOSTICS, NOT BEHAVIOUR. backtrace() reporting zero
 * frames means an SDK exception handler prints its message with no stack under
 * it. Everything about catching and reporting the exception still works; only
 * the frame list is lost. That is worth knowing before debugging a dataplane
 * crash and wondering why the backtrace is empty -- it is empty here, always,
 * and the answer is to use the fault address with the cross objdump instead
 * (see the ptmalloc_init diagnosis in the toolchain notes for that technique).
 *
 * Deliberately NOT implemented for real. A correct MIPS n64 stack unwinder is a
 * real piece of work, and writing a half-correct one that produces plausible
 * wrong frames would be worse than producing none.
 */
#ifndef FFN_COMPAT_EXECINFO_H
#define FFN_COMPAT_EXECINFO_H

#include <stddef.h>

static inline int backtrace(void **buffer, int size)
{
	(void)buffer;
	(void)size;
	return 0;
}

static inline char **backtrace_symbols(void *const *buffer, int size)
{
	(void)buffer;
	(void)size;
	return NULL;
}

static inline void backtrace_symbols_fd(void *const *buffer, int size, int fd)
{
	(void)buffer;
	(void)size;
	(void)fd;
}

#endif /* FFN_COMPAT_EXECINFO_H */
