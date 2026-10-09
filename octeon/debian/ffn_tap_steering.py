#!/usr/bin/env python3
"""Receive packet steering for the dataplane TAP devices.

A native owner writes every frame it accepts into its TAP with one write()
per frame, and the kernel forwards that frame inside the write: VLAN demux,
conntrack, the policy and NAT tables, routing and the queue of the egress
TAP. Without steering all of it runs on the writer's CPU, so one direction
of transit is bounded by one OCTEON core (measured 2026-10-09: 42.6 MB/s
through the box with the WAN owner's receive CPU at 97%). Receive packet
steering (RPS) hands the forwarding to other CPUs by flow hash, which keeps
each flow in order, and leaves the writer with the copy alone: 53-55 MB/s
for one flow and 63 MB/s for four, no core above 65%. PACKET-PERFORMANCE.md
has the measurements.

The steering CPUs are the upper half of the CPUs the owner may run on, less
CPU 0 and every CPU a native owner has reserved for its workers (the
reservation records in /run/ffn-packet-cpus, live or not). A machine with
fewer than eight eligible CPUs is left unsteered. The mask is written to
every receive queue of the device inside the forwarding namespace; an
owner applies it when it starts its workers and never fails an attachment
over it.
"""
import json
import os
from pathlib import Path
import re
import subprocess

DEVICE = re.compile(r'^(p[1-9][0-9]{0,2}|ae[1-9][0-9]{0,2})$')
NAMESPACE_NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
NAMESPACE = 'ffn-data'
RESERVATIONS = Path('/run/ffn-packet-cpus')
MINIMUM = 8


def reserved_cpus(root=RESERVATIONS):
    """Every CPU a native owner's reservation record names, live or stale."""
    cpus = set()
    for path in Path(root).glob('*.json'):
        try:
            for cpu in json.loads(path.read_text()).get('cpus', []):
                if type(cpu) is int and cpu >= 0:
                    cpus.add(cpu)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return cpus


def steering_cpus(allowed, reserved=()):
    """The upper half of the eligible CPUs, or nothing on a small machine."""
    reserved = set(reserved)
    candidates = sorted(c for c in set(allowed) if type(c) is int and c > 0 and c not in reserved)
    if len(candidates) < MINIMUM:
        return []
    return candidates[len(candidates) // 2:]


def mask(cpus):
    """The kernel's bitmap text: 32-bit hex words, most significant first, comma separated."""
    value = 0
    for cpu in cpus:
        if type(cpu) is not int or not 0 <= cpu < 4096:
            raise ValueError('Invalid steering CPU')
        value |= 1 << cpu
    words = []
    while True:
        words.append('%08x' % (value & 0xffffffff))
        value >>= 32
        if not value:
            break
    return ','.join(reversed(words))


def apply(device, cpus, namespace=NAMESPACE, run=subprocess.run):
    """Write the mask to every receive queue of the device inside its namespace."""
    if not isinstance(device, str) or not DEVICE.match(device):
        raise ValueError('Invalid steering device')
    if not isinstance(namespace, str) or not NAMESPACE_NAME.match(namespace):
        raise ValueError('Invalid network namespace')
    value = mask(cpus)
    script = ('for q in /sys/class/net/%s/queues/rx-*; do echo %s > "$q/rps_cpus" || exit 1; done'
              % (device, value))
    result = run(['ip', 'netns', 'exec', namespace, 'sh', '-c', script],
                 capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise OSError('receive steering for %s failed: %s' % (device, (result.stderr or '').strip()[:200]))
    return value


def steer(device, namespace=NAMESPACE, allowed=None, reserved=None, run=subprocess.run):
    """Choose the steering CPUs for this process and apply them; [] means the device is unsteered."""
    allowed = os.sched_getaffinity(0) if allowed is None else allowed
    reserved = reserved_cpus() if reserved is None else reserved
    cpus = steering_cpus(allowed, reserved)
    apply(device, cpus, namespace, run)
    return cpus


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Apply receive steering to a dataplane TAP device.')
    parser.add_argument('device')
    parser.add_argument('--namespace', default=NAMESPACE)
    args = parser.parse_args(argv)
    cpus = steer(args.device, args.namespace)
    print(json.dumps(dict(device=args.device, cpus=cpus, mask=mask(cpus))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
