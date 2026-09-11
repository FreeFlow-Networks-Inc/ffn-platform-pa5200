#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Prove a staged DP kernel carries the mailbox agent BEFORE booting it.

WHY THIS EXISTS
---------------
A DP that boots without ``/sbin/ffn_dpagent2`` comes up cleanly on 40 cores and
is **completely unreachable**: the DP has no console, no management network of
its own, and ``ffn-dpsh`` over the PCIe mailbox is the only way in. That has
already happened once -- see ``../DP-NFSROOT.md`` -- and cost a full recovery
cycle against a previously staged kernel. This is the cheap check that makes it
not happen again.

WHY ``strings`` IS THE WRONG TOOL
---------------------------------
``strings`` on the vmlinux finds **nothing**, not even for a kernel whose agent
is provably running at that moment. The initramfs is an embedded *xz-compressed*
cpio, so a zero from ``strings`` is not evidence of absence -- it is evidence
that the check was wrong, which is the more dangerous of the two. Both DP
kernels on this appliance return zero for all five markers and both are fine.

So the blob is located by compression magic, decompressed, and the real bytes
are searched. An inconclusive result is reported as inconclusive and exits
non-zero; it is never reported as a pass.

Usage::

    ffn_dp_kernel_check.py /opt/ffn/ffn-vmlinux-6.18.49-dp-nfsroot

Read-only. Boots nothing, touches no hardware.
"""
import gzip
import io
import lzma
import sys
import zlib

# The first three are the agent branch: without them the DP is unreachable and
# booting is a mistake. The last two are the NFS-root flow, which is optional --
# a kernel without them simply boots on its initramfs.
AGENT = [b"/sbin/ffn_dpagent2", b"DP session agent", b"restarting it"]
NFSROOT = [b"/sbin/ffn-nfsroot", b"ffn-switch-root"]
WANT = AGENT + NFSROOT

# Magic -> (name, decompressor). lz4 and zstd have no stdlib decoder; they are
# listed so an unrecognised-but-known format is reported rather than silently
# skipped, which would look identical to "no initramfs here".
MAGICS = [
    (b"\xfd7zXZ\x00", "xz",
     lambda b: lzma.LZMADecompressor(format=lzma.FORMAT_XZ).decompress(b)),
    (b"\x1f\x8b\x08", "gzip",
     lambda b: zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(b)),
    (b"\x5d\x00\x00", "lzma",
     lambda b: lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(b)),
    (b"\x04\x22\x4d\x18", "lz4", None),
    (b"\x28\xb5\x2f\xfd", "zstd", None),
    (b"070701", "cpio (uncompressed)", lambda b: b),
]


def find_all(hay, needle, limit=64):
    out, i = [], 0
    while len(out) < limit:
        i = hay.find(needle, i)
        if i < 0:
            break
        out.append(i)
        i += 1
    return out


def decompress(blob, dec):
    """Return what decodes, or None.

    A truncated tail is EXPECTED and not an error: the compressed initramfs is
    followed by the rest of the kernel image, so every decoder runs off the end
    of valid data. The streaming decoders keep what they already produced, which
    is why they are used in preference to the one-shot helpers.
    """
    if dec is None:
        return None
    try:
        return dec(blob)
    except Exception:
        return None


def looks_like_initramfs(blob):
    # newc cpio headers, or the archive terminator. Without this a random
    # compressed blob elsewhere in the image could be reported as the initramfs.
    return b"070701" in blob[:200000] or b"TRAILER!!!" in blob


def check(path):
    """Return (hits, found_blob)."""
    with open(path, "rb") as fh:
        img = fh.read()
    print("=== %s" % path)
    print("    %d bytes" % len(img))

    for magic, name, dec in MAGICS:
        for off in find_all(img, magic):
            blob = decompress(img[off:], dec)
            if not blob or len(blob) < 4096 or not looks_like_initramfs(blob):
                continue
            hits = {w: len(find_all(blob, w)) for w in WANT}
            if not any(hits.values()):
                continue
            print("    %s initramfs at 0x%x -> %d bytes" % (name, off, len(blob)))
            for w in WANT:
                print("      %-22s x%d" % (w.decode(), hits[w]))
            names = sorted(set(
                s for s in blob.split(b"\x00")
                if s.startswith(b"sbin/") and 5 < len(s) < 40))
            if names:
                print("      sbin/: %s"
                      % b" ".join(names[:16]).decode("ascii", "replace"))
            return hits, True
    return {w: 0 for w in WANT}, False


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: %s <vmlinux> [<vmlinux> ...]\n" % argv[0])
        return 2
    ok = True
    for path in argv[1:]:
        try:
            hits, found = check(path)
        except IOError as exc:
            print("    cannot read: %s" % exc)
            ok = False
            continue
        if not found:
            print("    INCONCLUSIVE: no initramfs blob could be decompressed.")
            print("    This is NOT a pass. Do not read it as evidence the agent")
            print("    is present -- find out why the blob was not located.")
            ok = False
        elif [w for w in AGENT if not hits[w]]:
            print("    MISSING AGENT: %s"
                  % ", ".join(w.decode() for w in AGENT if not hits[w]))
            print("    Booting this would strand the DP. Do not boot it.")
            ok = False
        else:
            print("    agent branch PRESENT")
            if all(hits[w] for w in NFSROOT):
                print("    nfsroot flow PRESENT (mounts and hands over to pid 1)")
            elif hits[NFSROOT[0]]:
                print("    note: has /sbin/ffn-nfsroot but no ffn-switch-root --")
                print("    it can mount, but pid 1 will not perform the handover,")
                print("    so the DP stays on its initramfs.")
            else:
                print("    note: no nfsroot flow; this kernel boots on initramfs")
        print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
