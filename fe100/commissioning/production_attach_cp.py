#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""Production attachments in the FE100: the table-only first stage of inline offload.

For each production interface this programs, on the CP, what the chip needs before a
single production packet is steered into it: the RX/TX port maps of its logical
ports, one miss next hop that sends unknown flows to the DP trunk as FLOWUNKNOWN
messages, and (through AttachmentOwner, journaled in SQLite) one LIF ingress
selector per port+VLAN carrying the zone and that miss next hop, plus one LEF
egress entry per port. Nothing here changes BCM steering, so traffic is unaffected
until the steering stage deliberately switches ports 16/32/33 toward port 3.

Runs on the CP beside first_session_cp.py, whose lock, worker and table machinery
it reuses:

  python3 production_attach_cp.py status            # readiness, port maps, occupancy, journal
  python3 production_attach_cp.py install < plan    # idempotent: skips what is already owned
  python3 production_attach_cp.py remove            # reverse, only what this journal owns
  python3 production_attach_cp.py plan              # print the default plan

Outputs JSON. Every write is journaled before it happens and read back after.
"""
import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path[:0] = ['/usr/local/sbin', '/usr/local/lib/ffn', str(Path(__file__).resolve().parent)]
import first_session_cp as fs
from ffn_fe100_attachments import AttachmentOwner, NativeEncoder

ROOT = fs.ROOT
JOURNAL = ROOT / 'production-attachments.sqlite3'
RECORD = ROOT / 'production-attachments.json'

# Zone numbers the FE100 sees. The lab reserves 4093/4094; these are the production
# zones of the committed policy, one number per Security zone, stable across runs.
ZONES = {'WAN': 10, 'LAN': 20, 'GUEST-VLAN': 30, 'VOICE-VLAN': 40, 'OSP-INTERNAl-ZONE': 50, 'OSP-WAN-ZONE': 60,
         'LAB': 99}    # the lab front port, for return-path proofs; never a policy zone
# Every VLAN the LAG carries gets a LIF in the LAN chip-zone (20): the chip's zone only scopes its own
# lookups, one binding per member port then covers all of them, and the kernel keeps enforcing the real
# zone policy on the software path while admission checks interface pairs itself.
LAG_VLANS = (0, 17, 69, 101, 375, 444, 447)

DEFAULT_PLAN = {
    'schema': 1,
    # The next hop's destination is the chip's 4-bit CPU message target, not a BCM port: the lab's 8 is
    # what the DP decoder expects in the retained ITMH; BCM port 20's force-forward decides delivery (-> 24).
    'miss_sysport': 8,
    'miss_dmac': '02:52:20:ab:cd:ee',
    'pools': {'lif': list(range(2, 28)), 'lef': list(range(2, 28)), 'nexthop': list(range(2, 16)),
              'smac': list(range(2, 8)), 'qm': list(range(2, 20)), 'acl': list(range(2, 16)),
              'rxport': [5, 21, 22], 'txport': [5, 21, 22]},
    # FE100 logical port -> BCM physical port (FRONT in ffn_dp_packet_transport).
    'rxport_physical': {'5': 16, '21': 32, '22': 33},
    'attachments': [{'interface': 'ethernet1/5', 'zone_name': 'WAN', 'vlan': 0, 'ports': [5]}] +
                   [{'interface': 'ae1' if v == 0 else 'ae1.%d' % v, 'zone_name': 'LAN', 'vlan': v, 'ports': [21, 22]} for v in LAG_VLANS],
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def owner_plan(plan, attachment, miss_next_hop):
    """The AttachmentOwner's plan shape for one interface."""
    zone = ZONES[attachment['zone_name']]
    base = dict(interface=attachment['interface'], zone=zone, vlan=int(attachment['vlan']),
                ports=sorted(set(int(p) for p in attachment['ports'])), miss_next_hop=int(miss_next_hop))
    return dict(base, binding_revision=digest(dict(base, plan=plan['schema'], miss_sysport=plan['miss_sysport'])))


def steering_withdrawn(ports, physical):
    """True when none of the attachment's BCM ports is force-forwarded to the FE100 (port 3)."""
    from ffn_faceplate import call
    from ffn_fe100_bcm_lab import execute
    body = ''.join(' {int dst=0;int en=0;int rv=bcm_port_force_forward_get(0,%d,&dst,&en);'
                   'printf("FFN_FF port=%d destination=%%d enabled=%%d rv=%%d\\n",dst,en,rv);}\n' % (physical[str(p)], physical[str(p)])
                   for p in ports)
    result = execute('{' + body + ' printf("FFN_DONE\\n");}', call)
    for marker in result.get('markers', []):
        if marker.startswith('FFN_FF '):
            fields = dict(kv.split('=') for kv in marker.split()[1:])
            if fields['rv'] != '0' or (fields['enabled'] == '1' and fields['destination'] == '3'):
                return False
    return True


class Production(fs.Run):
    def __init__(self):
        super().__init__()
        self.path = ROOT / ('production-attach-' + str(time.time_ns()) + '.json')
        self.record['kind'] = 'production-attachments'
        self.save()
        self.db = sqlite3.connect(str(JOURNAL), isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.isolation_level = ''
        self.owner = None

    def durable(self):
        """The long-lived record of what production attachments own (survives runs)."""
        try:
            return json.loads(RECORD.read_text())
        except (OSError, ValueError):
            return {'schema': 1, 'miss_next_hop': None, 'port_maps': [], 'attachments': {}}

    def save_durable(self, data):
        temp = RECORD.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(data, f, indent=1); f.flush(); os.fsync(f.fileno())
        os.replace(temp, RECORD)

    def attachment_owner(self, plan):
        if self.owner is None:
            self.call('lif', index=plan['pools']['lif'][0])          # creates the ResourceTables backend
            boot = self.record['cp_boot_id']
            plans = {a['interface']: owner_plan(plan, a, self.durable().get('miss_next_hop') or plan['pools']['nexthop'][0])
                     for a in plan['attachments']}
            current = lambda name: copy.deepcopy(plans[name])
            withdraw = lambda name: steering_withdrawn(plans[name]['ports'], plan['rxport_physical'])
            drain = lambda name: True   # no admission path exists yet; nothing references these LIFs/LEFs
            self.owner = AttachmentOwner(self.db, self.resources, dict(lif=plan['pools']['lif'], lef=plan['pools']['lef']),
                                         boot, current, withdraw, drain, NativeEncoder())
            self.plans = plans
        return self.owner

    def status(self, plan):
        self.plan = plan
        readiness = self.call('readiness')
        maps = {}
        for logical in plan['pools']['rxport']:
            maps['rxport/%d' % logical] = self.call('rxport', index=logical)
            maps['txport/%d' % logical] = self.call('txport', index=logical)
        occupancy = {}
        for kind in ('lif', 'lef', 'nexthop'):
            occupancy[kind] = [i for i in plan['pools'][kind] if self.call(kind, index=i)['rc'] == 0]
        owner = self.attachment_owner(plan)
        return dict(readiness=readiness, port_maps=maps, occupancy=occupancy, durable=self.durable(),
                    owned=sorted(owner.rows), recovery_required=owner.recovery_required,
                    steering={a['interface']: steering_withdrawn(a['ports'], plan['rxport_physical']) for a in plan['attachments']})

    def install(self, plan):
        self.plan = plan
        readiness = self.call('readiness')
        if not readiness.get('ready', False) and readiness.get('commissioning_blockers'):
            raise RuntimeError('hardware readiness: ' + json.dumps(readiness)[:300])
        durable = self.durable()
        # 1. port maps (pre-existing identical entries are fine and recorded as such)
        for logical in plan['pools']['rxport']:
            self.ensure('rxport', logical, bytes((logical,)))
            self.ensure('txport', logical, bytes((0, 0, plan['rxport_physical'][str(logical)])))
            if logical not in durable['port_maps']:
                durable['port_maps'].append(logical)
        self.save_durable(durable)
        # 2. the miss next hop toward the DP trunk
        if durable.get('miss_next_hop') is None:
            index = plan['pools']['nexthop'][0]
            if self.call('nexthop', index=index)['rc'] == 0:
                raise RuntimeError('miss next-hop slot %d is occupied' % index)
            self.write('nexthop', index, fs.next_hop(destination=plan['miss_sysport'], dmac=plan['miss_dmac']))
            durable['miss_next_hop'] = index
            self.save_durable(durable)
        # 3. LIF/LEF per attachment through the journaled owner
        owner = self.attachment_owner(plan)
        recovered = []
        if owner.recovery_required:
            # The owner never adopts rows across a process restart: it verifies and releases
            # what the journal says it owned, then this run acquires the plan afresh (same
            # pools, same order, so the indices come back identical while nothing else owns them).
            recovered = sorted(owner.rows)
            owner.recover()
            for name in recovered:
                durable['attachments'].pop(name, None)
            self.save_durable(durable)
        results = {}
        for a in plan['attachments']:
            name = a['interface']
            if name in owner.rows:
                results[name] = dict(owner.snapshot(name), already=True)
            else:
                results[name] = owner.acquire(name, self.plans[name])
            durable['attachments'][name] = dict(plan=self.plans[name], ingress_lifs=results[name]['ingress_lifs'],
                                                egress_lifs=results[name]['egress_lifs'], zone=self.plans[name]['zone'])
            self.save_durable(durable)
        self.record['stage'] = 'installed'; self.save()
        return dict(miss_next_hop=durable['miss_next_hop'], attachments=results, port_maps=durable['port_maps'], recovered=recovered)

    def remove(self, plan):
        self.plan = plan
        durable = self.durable()
        owner = self.attachment_owner(plan)
        released = []
        for name in list(owner.rows):
            owner.release(name)
            durable['attachments'].pop(name, None); released.append(name)
            self.save_durable(durable)
        if durable.get('miss_next_hop') is not None:
            index = durable['miss_next_hop']
            current = self.call('nexthop', index=index)
            if current['rc'] == 0:
                self.call('nexthop', 'delete', index)
            durable['miss_next_hop'] = None
            self.save_durable(durable)
        # port maps are left in place: they are shared with the lab/first-session tooling and harmless
        self.record['stage'] = 'removed'; self.save()
        return dict(released=released, remaining=self.durable())


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'plan':
        print(json.dumps(DEFAULT_PLAN, indent=1)); return
    if action not in ('status', 'install', 'remove'):
        raise SystemExit('usage: production_attach_cp.py plan|status|install|remove')
    plan = copy.deepcopy(DEFAULT_PLAN)
    if action == 'install' and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            plan = json.loads(text)
    run = Production()
    try:
        result = getattr(run, action)(plan)
        print(json.dumps(dict(ok=True, action=action, journal=str(run.path), result=result), default=str))
    except Exception as error:
        print(json.dumps(dict(ok=False, action=action, journal=str(run.path), error=str(error))))
        raise SystemExit(1)
    finally:
        run.close()


if __name__ == '__main__':
    main()
