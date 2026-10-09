#!/usr/bin/env python3
"""Lifetime-bound packet qualification for the FE100 control owner.

The isolated harness (`validate_front_sessions.py`, run on the MP) proves on
this silicon that an installed session forwards through the switch steering
and that the FE100 rewrites NAT tuples and checksums exactly;
`validate_nat_results.py` audits those reports and binds every case to one
hardware lifetime: the CP boot, the BCM owner epoch and the production
aggregate owners. This module keeps the audited result where the owner can
trust it, and nowhere else:

* the record lives on the CP's tmpfs, so it cannot outlive the CP boot (the
  FE100 personality is reloaded on every boot);
* every read re-checks the lifetime against the live CP boot and BCM owner
  epoch. A switch daemon restart or a reboot invalidates the record, and the
  owner treats that like a changed attachment: drain, then blocked;
* the record states its scope. `external-wire` is the complete eight-case
  matrix over the front5/front13 loop; `internal-loop` is the cable-free MAC
  loop on an unconfigured port, both protocols, address-plus-port, both
  directions. Either lifts the per-session front-port and NAT gates. Neither
  activates a policy: activation stays the operator's explicit operation.

Submit from the MP, where the reports are:

    python3 validate_nat_results.py --internal R1.json R2.json R3.json R4.json \\
      | ssh -F /etc/ffn-ngfw/ssh-cp.conf ffn-cp \\
          python3 /usr/local/sbin/ffn_fe100_qualification.py submit --scope internal-loop
"""
import json
import os
import sys
import time
from pathlib import Path

RECORD = Path('/run/ffn-fe100/qualification.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
SCOPES = ('external-wire', 'internal-loop')
# The auditor's external matrix: both protocols, both translations, both front ports.
EXTERNAL_CASES = frozenset((p, t, i) for p in ('udp', 'tcp') for t in ('address', 'port') for i in (5, 13))
# The internal loop cannot swap physical ports; it must cover both protocols,
# address-plus-port translation and both tuple directions.
INTERNAL_CASES = frozenset((p, 'port', r) for p in ('udp', 'tcp') for r in (False, True))
CASE_FIELDS = frozenset(('protocol', 'translation', 'ingress', 'egress', 'packets', 'internal_mac_loopback',
                         'source_mac_rewrite', 'nat_reverse', 'cp_boot_id', 'bcm_epoch', 'production_owners'))
KEPT = ('protocol', 'translation', 'ingress', 'egress', 'packets', 'nat_reverse', 'source_mac_rewrite')


def live_lifetime():
    """The lifetime a record must name: this CP boot and the one BCM owner process."""
    from ffn_copper_forwarding import epoch
    return dict(cp_boot_id=BOOT_ID.read_text().strip(), bcm_epoch=epoch())


def accept(summary, scope, lifetime, now=None):
    """Turn the auditor's summary into a record bound to `lifetime`, or raise ValueError."""
    if scope not in SCOPES:
        raise ValueError('qualification scope must be one of: ' + ', '.join(SCOPES))
    if not isinstance(summary, dict) or summary.get('schema') != 1:
        raise ValueError('auditor summary schema 1 required')
    if summary.get('production_admission') is not False:
        raise ValueError('the auditor never authorises production admission')
    cases = summary.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('no audited cases')
    for case in cases:
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise ValueError('audited case fields differ from the auditor')
    lifetimes = {(c['cp_boot_id'], c['bcm_epoch'], json.dumps(c['production_owners'], sort_keys=True)) for c in cases}
    if len(lifetimes) != 1:
        raise ValueError('cases cross hardware or production owner lifetimes')
    boot, epoch, owners = next(iter(lifetimes))
    if (boot, epoch) != (lifetime['cp_boot_id'], lifetime['bcm_epoch']):
        raise ValueError('cases were audited in another CP boot or BCM owner lifetime')
    internal = {c['internal_mac_loopback'] for c in cases}
    if scope == 'external-wire':
        if internal != {False}:
            raise ValueError('external-wire scope accepts wire cases only')
        if summary.get('complete') is not True:
            raise ValueError('external-wire scope requires the complete matrix')
        if {(c['protocol'], c['translation'], c['ingress']) for c in cases} != EXTERNAL_CASES:
            raise ValueError('external-wire matrix differs from the eight audited cases')
    else:
        if internal != {True} or summary.get('external_wire_verified') is not False:
            raise ValueError('internal-loop scope accepts internal MAC loop cases only')
        missing = INTERNAL_CASES - {(c['protocol'], c['translation'], bool(c['nat_reverse'])) for c in cases}
        if missing:
            raise ValueError('internal-loop scope is missing: ' + ', '.join(
                '%s %s %s' % (p, t, 'reverse' if r else 'forward') for p, t, r in sorted(missing)))
    return dict(schema=1, scope=scope, cp_boot_id=boot, bcm_epoch=epoch, production_owners=json.loads(owners),
                cases=[{k: c[k] for k in KEPT} for c in cases], sources=summary.get('sources') or [],
                recorded_at=time.time() if now is None else now)


class Qualification:
    """The owner-side view of the record, bound to the live lifetime on every read."""

    def __init__(self, path=None, lifetime=None, clock=time.time):
        self.path = Path(path or RECORD)
        self.lifetime = lifetime or live_lifetime
        self.clock = clock

    def record(self, summary, scope):
        row = accept(summary, scope, self.lifetime(), self.clock())
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        with open(temp, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            json.dump(row, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
        return self.current()

    def load(self):
        # Read every time: the record is small, the owner asks every few
        # seconds, and file timestamps are too coarse to cache on.
        try:
            row = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(row, dict) or row.get('schema') != 1 or row.get('scope') not in SCOPES:
            return None
        return row

    def current(self):
        base = dict(front_port=False, nat_rewrite=False, scope=None, cp_boot_id=None, bcm_epoch=None, cases=0,
                    recorded_at=None, reason=None)
        row = self.load()
        if row is None:
            return dict(base, reason='no qualification record for this CP boot')
        try:
            live = self.lifetime()
        except Exception as error:
            return dict(base, scope=row['scope'], reason='hardware lifetime unavailable: ' + str(error)[:160])
        if (row.get('cp_boot_id'), row.get('bcm_epoch')) != (live['cp_boot_id'], live['bcm_epoch']):
            return dict(base, scope=row['scope'], reason='qualification record is from another CP boot or BCM owner lifetime')
        return dict(base, front_port=True, nat_rewrite=True, scope=row['scope'], cp_boot_id=row['cp_boot_id'],
                    bcm_epoch=row['bcm_epoch'], cases=len(row.get('cases') or []), recorded_at=row.get('recorded_at'))

    def front_port(self):
        return self.current()['front_port'] is True

    def nat(self):
        return self.current()['nat_rewrite'] is True

    def health(self):
        """What the session adapter merges into its health view before an insert."""
        return dict(nat_offload_verified=self.nat())


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest='action', required=True)
    submit = actions.add_parser('submit', help='record the auditor summary read from stdin through the control owner')
    submit.add_argument('--scope', choices=SCOPES, required=True)
    actions.add_parser('show', help='print the record as the owner sees it now')
    args = parser.parse_args(argv)
    for directory in ('/usr/local/sbin', '/usr/local/lib/ffn'):
        if directory not in sys.path:
            sys.path.append(directory)
    if args.action == 'show':
        print(json.dumps(Qualification().current(), indent=2))
        return 0
    data = sys.stdin.buffer.read((1 << 20) + 1)
    if len(data) > (1 << 20):
        raise ValueError('auditor summary exceeds 1 MiB')
    from ffn_fe100_policy_control import dispatch
    print(json.dumps(dispatch('qualify', dict(scope=args.scope, summary=json.loads(data))), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
