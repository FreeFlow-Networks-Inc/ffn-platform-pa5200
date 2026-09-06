/* SPDX-License-Identifier: GPL-2.0-or-later
 * Copyright (C) 2026 FreeFlow Networks, Inc.
 *
 * sys/sysmips.h -- musl compatibility shim.
 *
 * The SDK's cvmx-platform.h includes <sys/sysmips.h> unconditionally for a
 * LINUX_USER build, to call
 *
 *     sysmips(MIPS_CAVIUM_XKPHYS_WRITE, getpid(), 3, 0)
 *
 * That header is a glibc extension. musl does not ship it, and musl is what the
 * dataplane must link against: the DP's initramfs is static busybox with no
 * shared libraries at all, and the SDK's own 2012 static glibc segfaults in
 * ptmalloc_init on a modern kernel before reaching main().
 *
 * The syscall itself exists in every MIPS kernel -- only the userspace wrapper
 * is missing -- so this declares it and routes it through syscall(2).
 *
 * WHAT IT WILL RETURN HERE, so nobody debugs it twice: -EINVAL. Upstream's
 * sysmips implements only MIPS_ATOMIC_SET and MIPS_FIXADE, and the Cavium
 * XKPHYS commands are a vendor kernel patch we did not take. That failure is
 * harmless: cvmx_linux_enable_xkphys_access() only perror()s and returns void,
 * and octeon/kctl/ffn_xkphys.c has already set CvmMemCtl[xkmemenau,xkioenau] on
 * every core, so the access the call was asking for is granted before the
 * program starts.
 *
 * Expect one "sysmips(MIPS_CAVIUM_XKPHYS_WRITE) failed: Invalid argument" on
 * stderr at startup. It is noise, not a fault.
 */
#ifndef FFN_COMPAT_SYS_SYSMIPS_H
#define FFN_COMPAT_SYS_SYSMIPS_H

#include <stdarg.h>
#include <unistd.h>
#include <sys/syscall.h>

#ifndef SYS_sysmips
/* n64 ABI. musl defines SYS_sysmips on mips64, but do not depend on it: an
 * absent number would otherwise become a confusing compile error inside an SDK
 * header rather than here.
 */
#define SYS_sysmips 5199
#endif

/*
 * VARARGS, matching glibc's `extern int sysmips (const int __cmd, ...)`.
 *
 * This is not stylistic. The SDK calls it with different arity from different
 * places -- cvmx-platform.h:167 passes four arguments, cvmx-tim.h:380 passes
 * three -- so a fixed-arity declaration compiles one caller and rejects the
 * other with "too few arguments to function 'sysmips'". A first version of this
 * header took four fixed arguments and broke cvmx-config-init and
 * cvmx-helper-fpa exactly that way.
 *
 * Reading three arguments unconditionally is safe on n64: variadic arguments
 * arrive in registers, so an argument the caller did not pass yields whatever
 * was in that register rather than faulting, and the kernel ignores arguments
 * the command does not use.
 */
static inline int sysmips(const int cmd, ...)
{
	va_list ap;
	long a1, a2, a3;

	va_start(ap, cmd);
	a1 = va_arg(ap, long);
	a2 = va_arg(ap, long);
	a3 = va_arg(ap, long);
	va_end(ap);

	return (int)syscall(SYS_sysmips, (long)cmd, a1, a2, a3);
}

#endif /* FFN_COMPAT_SYS_SYSMIPS_H */
