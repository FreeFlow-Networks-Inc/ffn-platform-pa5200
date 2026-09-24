#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Check that the chip is running the SOC properties we declare.

ffn-bcm-overrides.conf is what FFN says the BCM88375 should be configured with.
A `config show` capture in bcm/BCM-CONFIG-*.json is what the chip's SDK property
store actually held. Nothing kept those two in agreement, and they had already
diverged:

  * `tm_port_header_type_in_8` was declared RAW while the live store said ETH,
  * ports 5 and 24 were running ETH with no declaration at all -- hand-edited
    into the staged tree after each init and lost on every re-init.

That is not a cosmetic difference. The port class decides whether
`bcm_port_stp_set` works at all, and whether a port reads an ingress TM header
-- so a stale declaration silently invalidates both the L2 path and every ITMH
experiment, and the only symptom is a confusing result much later.

WHAT COUNTS AS A FAILURE, AND WHY THE TWO CASES DIFFER

  MISMATCH   declared X, the chip holds Y.  Unambiguous: one of the two is
             wrong and somebody has to decide which. Fails.
  ABSENT     declared, not in the capture at all.  Reported, and NOT fatal by
             default -- `config show` enumerates the store, so an absent key
             usually means the property was declared after the last chip init
             and simply has not been staged yet. That is worth seeing in the
             log without blocking a change made elsewhere in the tree. Use
             --strict to make it fatal.

This is deliberately a comparison against a CHECKED-IN capture rather than a
live chip: CI has no hardware, and the point is to catch a declaration going
stale relative to the last thing we actually observed. Refresh the capture when
the chip is re-initialised.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
OVERRIDES = os.path.join(HERE, "ffn-bcm-overrides.conf")
SUFFIX = ".BCM88650"


def read_overrides(path):
    """Declared key/value pairs, comments and blank lines removed."""
    out = []
    with open(path) as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            key, val = line.split("=", 1)
            out.append((key.strip(), val.strip(), lineno))
    return out


def read_capture(path):
    """Property store from a `config show` capture, as {key: value}."""
    with open(path) as fh:
        doc = json.load(fh)
    lines = doc.get("lines")
    if lines is None:
        raise ValueError("%s has no 'lines' -- is it a `config show` capture?"
                         % os.path.basename(path))
    if doc.get("truncated"):
        raise ValueError("%s is truncated; a partial store would report "
                         "spurious ABSENT results" % os.path.basename(path))
    return parse_store(lines)


def parse_store(lines):
    """`config show` output lines -> {key: value}."""
    store = {}
    for line in lines:
        line = line.strip()
        if not line or "=" not in line or line.startswith("#"):
            continue
        key, val = line.split("=", 1)
        store[key.strip()] = val.strip()
    return store


def alternate(key):
    """The other spelling: config.bcm uses both `key.BCM88650` and bare `key`."""
    return key[:-len(SUFFIX)] if key.endswith(SUFFIX) else key + SUFFIX


def compare(declared, store):
    ok, mismatch, absent = [], [], []
    for key, val, lineno in declared:
        if key in store:
            (ok if store[key] == val else mismatch).append((key, val, store[key], lineno))
        elif alternate(key) in store:
            # A real distinction, not a formatting quirk: the SDK treats the two
            # spellings as separate properties, so this is drift, not a match.
            mismatch.append((key, val, "%s=%s" % (alternate(key), store[alternate(key)]),
                             lineno))
        else:
            absent.append((key, val, lineno))
    return ok, mismatch, absent


def self_test():
    """The parsing, on synthetic input -- so a bad parser cannot pass quietly."""
    store = parse_store([
        "    load_firmware.BCM88650=0x1",
        "    tm_port_header_type_in_8.BCM88650=ETH",
        "  # a comment",
        "",
        "    private_ip_frwrd_table_size=0",
    ])
    assert store["load_firmware.BCM88650"] == "0x1", store
    assert store["tm_port_header_type_in_8.BCM88650"] == "ETH", store
    assert len(store) == 3, store
    assert alternate("phy_ext_rom_boot") == "phy_ext_rom_boot.BCM88650"
    assert alternate("load_firmware.BCM88650") == "load_firmware"
    declared = [("a", "1", 1), ("b", "2", 2), ("c", "3", 3)]
    ok, mis, ab = compare(declared, {"a": "1", "b": "9"})
    assert [x[0] for x in ok] == ["a"], ok
    assert [x[0] for x in mis] == ["b"], mis
    assert [x[0] for x in ab] == ["c"], ab
    # a value containing '=' must survive the split
    assert parse_store(["  k=a=b"])["k"] == "a=b"
    # the two spellings are separate properties, so this is drift, not a match
    _, mis, _ = compare([("phy_ext_rom_boot", "0", 1)],
                        {"phy_ext_rom_boot.BCM88650": "0"})
    assert len(mis) == 1, mis
    print("self-test: parsing and comparison OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--capture", help="config show capture "
                                      "(default: newest bcm/BCM-CONFIG-*.json)")
    ap.add_argument("--overrides", default=OVERRIDES)
    ap.add_argument("--strict", action="store_true",
                    help="treat a declared-but-absent property as a failure")
    a = ap.parse_args()

    self_test()

    cap = a.capture
    if not cap:
        found = sorted(glob.glob(os.path.join(REPO, "bcm", "BCM-CONFIG-*.json")))
        if not found:
            print("no bcm/BCM-CONFIG-*.json to compare against; nothing to check")
            return 0
        cap = found[-1]

    declared = read_overrides(a.overrides)
    store = read_capture(cap)
    ok, mismatch, absent = compare(declared, store)

    print("\noverrides : %s (%d declared)"
          % (os.path.relpath(a.overrides, REPO), len(declared)))
    print("capture   : %s (%d properties)"
          % (os.path.relpath(cap, REPO), len(store)))
    print("agree     : %d" % len(ok))

    for key, want, got, lineno in mismatch:
        print("\nDRIFT  %s" % key)
        print("  declared : %s   (%s:%d)"
              % (want, os.path.basename(a.overrides), lineno))
        print("  chip has : %s" % got)

    for key, want, lineno in absent:
        print("\nABSENT %s" % key)
        print("  declared : %s   (%s:%d)"
              % (want, os.path.basename(a.overrides), lineno))
        print("  chip has : nothing -- declared after the last init, or never "
              "staged. Re-run ffn-bcm-stage-config.sh and re-init.")

    if mismatch:
        print("\n%d property(ies) differ between what we declare and what the "
              "chip ran. Decide which is right: either correct the declaration, "
              "or re-stage and re-init the chip." % len(mismatch))
        return 1
    if absent and a.strict:
        print("\n%d declared property(ies) are not in the capture (--strict)."
              % len(absent))
        return 1
    print("\nno drift" + (" (%d absent, not fatal)" % len(absent) if absent else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
