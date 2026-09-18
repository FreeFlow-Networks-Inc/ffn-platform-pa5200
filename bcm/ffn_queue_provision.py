#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Decide what queue provisioning is still needed, from what the chip already has.

VOQ allocation on this switch is a one-shot manual commissioning step. The
existing recipe is a mode number compiled into ffn_bcm_forward_test.c, run once,
and COPPER-FORWARDING.md is blunt about what that costs:

    "tm-front4-allocate (64): ... This is a manual commissioning operation, not
     an idempotent provisioning loop. On a partial failure, inspect connectors,
     attachments and queues before cleanup; never retry blindly. It does not
     automatically unwind a partial allocation."

So every re-init is a hand operation, and a half-finished run leaves a state
nobody has named. This module names it.

WHAT MAKES THIS SAFE TO RUN TWICE

Not "allocate and ignore EEXIST" -- these calls do not work that way, and the
handles they return (connector 0xc4080038, VOQ 0x243c004c) are observations, not
identifiers to reuse after a restart. Idempotency here is achieved by
*deciding*, not by *retrying*: read what exists, compare it to what is wanted,
and emit the ports that still need work. Applying the plan stays with the
existing allocate path, one port at a time, under the same exclusive fabric lock.

The allocation already emits the facts needed, in the marker lines it prints:

    FFN_PORT      dst=24 core=0 tm=24 priorities=2 modid=0
    FFN_CONNECTOR dst=24 gport=0xc4000010 rv=0
    FFN_ATTACH    dst=24 queues=8 rv=0
    FFN_VOQ       dst=24 sysport=0x6c000018 gport=0x243c0004 rv=0
    FFN_DONE

Four stages per port, in that order. A port with all four and rv=0 everywhere is
done; a port with some of them is the dangerous case.

WHY A PARTIAL PORT IS NEVER PROPOSED FOR ALLOCATION

Because the allocate path does not unwind. A port that got a connector but no
VOQ already owns a scheduler resource, so running allocate again does not
resume -- it allocates a second connector and leaks the first. The plan reports
such a port as UNSAFE and stops, which is the machine-checkable form of "never
retry blindly". Deciding what to do about it needs a human looking at
connectors, attachments and queues.

MISSING FFN_DONE MEANS THE RUN WAS CUT, not that the remaining ports are absent:
the last port seen is indeterminate, and the file says so rather than guessing.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

STAGES = ("port", "connector", "attach", "voq")

COMPLETE = "complete"
MISSING = "missing"
PARTIAL = "partial"
FAILED = "failed"
MISMATCH = "mismatch"

_MARK = re.compile(r"^FFN_(PORT|CONNECTOR|ATTACH|VOQ)\s+dst=(\d+)\s*(.*)$")
_KV = re.compile(r"(\w+)=(\S+)")


def parse_markers(lines):
    """Marker lines -> ({port: {stage: {k: v}}}, done, order)."""
    ports, order, done = {}, [], False
    for raw in lines:
        line = raw.strip()
        if line.startswith("FFN_DONE"):
            done = True
            continue
        m = _MARK.match(line)
        if not m:
            continue
        stage, dst, rest = m.group(1).lower(), int(m.group(2)), m.group(3)
        rec = ports.setdefault(dst, {})
        if dst not in order:
            order.append(dst)
        rec[stage] = {k: v for k, v in _KV.findall(rest)}
    return ports, done, order


def load_observed(paths):
    """Merge several capture files. Later files win, which is why order matters."""
    ports, done, order, sources = {}, True, [], []
    for path in paths:
        with open(path) as fh:
            doc = json.load(fh)
        lines = doc.get("markers") or doc.get("output") or []
        got, got_done, got_order = parse_markers(lines)
        for dst, rec in got.items():
            ports.setdefault(dst, {}).update(rec)
        for dst in got_order:
            if dst not in order:
                order.append(dst)
        done = done and got_done
        sources.append((os.path.basename(path), len(got), got_done))
    return ports, done, order, sources


def rv_of(entry):
    try:
        return int(entry.get("rv", "0"), 0)
    except (TypeError, ValueError):
        return None


def classify(rec, want_queues):
    """One port's observed stages -> (state, detail)."""
    if not rec:
        return MISSING, "no allocation seen"

    for stage in STAGES:
        entry = rec.get(stage)
        if entry is None:
            continue
        rv = rv_of(entry)
        if rv not in (0, None):
            return FAILED, "FFN_%s returned rv=%d" % (stage.upper(), rv)

    present = [s for s in STAGES if s in rec]
    if len(present) != len(STAGES):
        reached = present[-1] if present else "nothing"
        nxt = STAGES[len(present)] if len(present) < len(STAGES) else "?"
        return PARTIAL, ("reached FFN_%s, no FFN_%s -- a resource is already "
                         "owned and allocate does not unwind"
                         % (reached.upper(), nxt.upper()))

    got = rec["attach"].get("queues")
    if want_queues is not None and got is not None and int(got) != int(want_queues):
        return MISMATCH, "attached %s queues, profile wants %s" % (got, want_queues)
    return COMPLETE, "voq %s sysport %s" % (rec["voq"].get("gport"),
                                            rec["voq"].get("sysport"))


def plan(observed, desired, done=True, order=()):
    """(port -> (state, detail)) for every port the profile asks for."""
    out = {}
    last_seen = order[-1] if order else None
    for port_s, spec in sorted(desired.items(), key=lambda kv: int(kv[0])):
        port = int(port_s)
        state, detail = classify(observed.get(port), spec.get("queues"))
        if not done and port == last_seen and state != COMPLETE:
            detail += "; the capture has no FFN_DONE, so this port is indeterminate"
        out[port] = (state, detail, spec)
    return out


def load_profile(path):
    with open(path) as fh:
        doc = json.load(fh)
    ports = doc.get("ports")
    if not isinstance(ports, dict) or not ports:
        raise ValueError("%s has no 'ports' object" % os.path.basename(path))
    return doc


def default_captures():
    found = []
    for pat in ("DP-QUEUES-ALLOCATION-*.json", "FE100-SESSION-QUEUES-*.json"):
        found += sorted(glob.glob(os.path.join(HERE, pat)))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default=os.path.join(HERE, "queue-profile.json"))
    ap.add_argument("--observed", nargs="*", help="allocation capture JSON(s); "
                                                  "later files win on conflict")
    ap.add_argument("--quiet", action="store_true", help="only print what needs work")
    a = ap.parse_args()

    profile = load_profile(a.profile)
    captures = a.observed if a.observed is not None else default_captures()
    if not captures:
        return "no allocation captures to read; pass --observed"
    observed, done, order, sources = load_observed(captures)

    print("profile  : %s (%d port(s))"
          % (os.path.basename(a.profile), len(profile["ports"])))
    for name, n, got_done in sources:
        print("observed : %-40s %2d port(s)%s"
              % (name, n, "" if got_done else "   NO FFN_DONE -- run was cut"))

    result = plan(observed, profile["ports"], done, order)
    buckets = {}
    for port, (state, detail, spec) in result.items():
        buckets.setdefault(state, []).append((port, detail, spec))

    for state in (COMPLETE, MISSING, PARTIAL, FAILED, MISMATCH):
        items = buckets.get(state)
        if not items or (a.quiet and state == COMPLETE):
            continue
        print("\n%s (%d)" % (state.upper(), len(items)))
        for port, detail, spec in items:
            role = spec.get("role", "")
            print("  bcm%-3d %-28s %s" % (port, role, detail))

    unsafe = buckets.get(PARTIAL, []) + buckets.get(FAILED, []) + buckets.get(MISMATCH, [])
    todo = buckets.get(MISSING, [])

    if unsafe:
        print("\n%d port(s) are in a state allocate cannot resume. Inspect "
              "connectors, attachments and queues by hand before doing anything "
              "else; do NOT re-run allocate for these." % len(unsafe))
        return 1
    if todo:
        print("\n%d port(s) still need allocating: %s"
              % (len(todo), ", ".join(str(p) for p, _, _ in todo)))
        print("Allocate them one at a time through the existing path, then "
              "re-run this against the new capture.")
        return 2
    print("\nconverged -- every port in the profile already has its queues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
