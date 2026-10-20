#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""The chip's return bindings for a packet owner: what the DP must know to accept
FE100 messages for its own front ports.

The FE100 answers a miss or an exception with a CPU message that the switch
delivers on the DP trunk (BCM port 20 force-forwarded to port 24, RAW). The
native decoder accepts such a message only for a binding the owner holds: the
trunk and return ports, the front port, the LIF the chip reports for it (its own
front port number) and the zone the attachment was commissioned with. Those
values come from the commissioning that programmed the attachment into the chip,
never from packets, and the owner renews them under a bounded lease every tick;
when the lease lapses or the file is gone, the native library stops accepting
returns while the ordinary path is untouched.

The commissioning writes one file for the whole dataplane:

    {"schema": 1, "trunk": 24, "return_port": 20, "lease_ms": 3000,
     "ports": {"5": {"in_lif": 5, "zone": 10}, "21": {"in_lif": 21, "zone": 20}}}

An owner reads it, keeps the entries for its own ports, and passes them with its
source system ports to the native library.
"""
import json
from pathlib import Path

CONFIG = Path('/etc/ffn/fe100-return.json')
DEFAULT_LEASE_MS = 3000


def load(path=CONFIG):
    """The validated file content, or None when there is nothing to bind (absent, empty or invalid)."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    try:
        validate(raw)
    except ValueError:
        return None
    return raw


def validate(raw):
    if not isinstance(raw, dict) or raw.get('schema') != 1:
        raise ValueError('unsupported return binding schema')
    for key in ('trunk', 'return_port'):
        if type(raw.get(key)) is not int or not 0 <= raw[key] <= 65535:
            raise ValueError('invalid ' + key)
    if raw['trunk'] == raw['return_port']:
        raise ValueError('trunk and return port must differ')
    lease = raw.get('lease_ms', DEFAULT_LEASE_MS)
    if type(lease) is not int or not 100 <= lease <= 10000:
        raise ValueError('lease outside 100..10000 ms')
    ports = raw.get('ports')
    if not isinstance(ports, dict) or len(ports) > 8:
        raise ValueError('ports must map at most eight front ports')
    for name, entry in ports.items():
        if not (isinstance(name, str) and name.isdigit() and 1 <= int(name) <= 63):
            raise ValueError('invalid front port ' + repr(name))
        if (not isinstance(entry, dict) or set(entry) != {'in_lif', 'zone'} or
                any(type(entry[k]) is not int or not 0 <= entry[k] <= 65535 for k in ('in_lif', 'zone'))):
            raise ValueError('invalid binding for front port ' + name)


def bindings(config, fronts, sources):
    """Native binding rows for this owner's front ports: [] when nothing applies.

    fronts: the owner's logical front ports; sources: {front: system port} (the FRONT map).
    """
    if not config:
        return []
    rows = []
    for front in sorted(fronts):
        entry = config['ports'].get(str(front))
        if entry is None or front not in sources:
            continue
        rows.append(dict(trunk=config['trunk'], return_port=config['return_port'], front=int(front),
                         in_lif=entry['in_lif'], zone=entry['zone'], source=int(sources[front])))
    return rows


def renew(owner, config, fronts, sources, monotonic_ms, state=None):
    """Apply the owner's bindings with a fresh lease, or revoke once when they are gone.

    Returns the state to pass next time: {'rows': [...], 'deadline_ms': int} or None.
    Calls owner.fe100(rows, deadline) only while rows exist (every tick, to renew the
    lease) or at the transition to none (to revoke). The caller holds the owner paused.
    """
    rows = bindings(config, fronts, sources)
    if rows:
        deadline = int(monotonic_ms) + (config.get('lease_ms', DEFAULT_LEASE_MS) if config else DEFAULT_LEASE_MS)
        owner.fe100(rows, deadline)
        return dict(rows=rows, deadline_ms=deadline)
    if state:
        owner.fe100([], 0)
    return None
