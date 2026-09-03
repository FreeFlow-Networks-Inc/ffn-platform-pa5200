#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Work around a GCC 13 ICE that stops OpenSSH building for mips64.

Symptom: five internal compiler errors building openssh-9.9p2, e.g.

    during RTL pass: zero_call_used_regs
    progressmeter.c:238:1: internal compiler error: in int_mode_for_mode,
                           at stor-layout.cc:407

That is GCC PR110934, and the trigger is OpenSSH's own hardening flag
-fzero-call-used-regs=used. OpenSSH's configure enables it because its probe
compiles a trivial function successfully; the pass only falls over on real
ones, so the failure surfaces at build time rather than configure time.

The consequence is not cosmetic. openssh fails, so no sshd is installed, so the
DP rootfs has no way in over the network -- which is exactly the state
output/target was found in.

Buildroot already models this bug as BR2_TOOLCHAIN_HAS_GCC_BUG_110934, but
declares it `default y if BR2_m68k` only, and wiring mips64 into that symbol
would pull in --without-hardening, which switches off OpenSSH's stack
protector, PIE, RELRO and trivial-auto-var-init as well. Dropping all of that
from the SSH daemon of a firewall to dodge one broken optimisation pass is a
bad trade.

So only the broken flag is disabled. -fno-zero-call-used-regs does not exist
(gcc rejects it outright); the spelling that works is -fzero-call-used-regs=skip,
and it has to appear AFTER the -fzero-call-used-regs=used that configure bakes
in, because last occurrence wins. OpenSSH compiles as `$(CC) $(CFLAGS)
$(CPPFLAGS)`, so CPPFLAGS is the slot that lands after CFLAGS. Verified on this
toolchain: both files that ICE compile clean with it appended.

Idempotent: re-running detects the marker and does nothing.
"""
import sys

MK = "/root/buildroot-2025.02.9/package/openssh/openssh.mk"
MARKER = "FFN: GCC PR110934"

BLOCK = """
# FFN: GCC PR110934 -- the zero_call_used_regs RTL pass ICEs on mips64 with
# gcc 13.x, so OpenSSH's -fzero-call-used-regs=used kills the build. Disable
# just that pass; CPPFLAGS is used because OpenSSH compiles with
# $(CC) $(CFLAGS) $(CPPFLAGS) and the last -fzero-call-used-regs wins.
# Buildroot's own BR2_TOOLCHAIN_HAS_GCC_BUG_110934 is deliberately not reused
# here: it implies --without-hardening, which would also drop the stack
# protector, PIE and RELRO from sshd.
ifeq ($(BR2_mips64)$(BR2_mips64el),y)
OPENSSH_CONF_ENV += CPPFLAGS="$(TARGET_CPPFLAGS) -fzero-call-used-regs=skip"
endif
"""

ANCHOR = "OPENSSH_DEPENDENCIES = host-pkgconf zlib openssl"


def main():
    src = open(MK).read()
    if MARKER in src:
        print("already patched")
        return 0
    if ANCHOR not in src:
        sys.stderr.write("anchor not found -- Buildroot/OpenSSH version changed?\n")
        return 1
    src = src.replace(ANCHOR, BLOCK.lstrip("\n") + "\n" + ANCHOR, 1)
    open(MK, "w").write(src)
    print("patched %s" % MK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
