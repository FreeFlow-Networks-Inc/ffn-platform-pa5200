#!/usr/bin/env python3
"""First production session in FE100 hardware: bounded, journaled, operator-driven CP controller.

--serve reads JSON commands on stdin while holding the FE100 table lock:
  {"op":"prepare","plan":{...}}  rx/tx port maps, LEFs, source MACs, next hops, LIFs, queue maps,
                                 ACL allows for ONE real session (addresses, MACs, ports, VLAN, zones
                                 and pools all come from the plan; nothing here is a default)
  {"op":"install"}               the NAT session pair (ADD then UPDATE, readback)
  {"op":"snapshot"}              FE100 counters
  {"op":"remove"}                the session pair
  {"op":"finish"}                restore every table change in reverse order
EOF, 120 s of silence or any failure restores. A durable journal records intent before writes;
a failed restore stays recorded as recovery_required. Hardware admission is NOT enabled by this:
the policy owner stays blocked; this is a commissioning run for one flow.
"""
import ctypes as C
import fcntl
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path[:0] = ['/usr/local/sbin', '/usr/local/lib/ffn']
from ffn_fe100 import register_map_path
from ffn_fe100_sessions import key4, entry4, output_key4
from ffn_fe100_session_adapter import encode_native, decode_native
from ffn_fe100_nexthop import LIB, SHA, encode as next_hop, encode_front, encode_smac
from ffn_fe100_nat import session_pair4

ROOT = Path('/var/lib/ffn/fe100')
WORKER_STATE = {}
COUNTERS = ('nif_p0_mac_rx_pkt_stats_ctr_no_rd_clr', 'nif_p0_mac_tx_pkt_stats_ctr_no_rd_clr',
            'lif_tlu_hit_stats_ctr_no_rd_clr', 'lif_tlu_miss_stats_ctr_no_rd_clr',
            'acl_tlu_hit_stats_ctr_no_rd_clr', 'acl_tlu_miss_stats_ctr_no_rd_clr',
            'dfp_flow_lkup_flow_hit_sta_ctr_no_rd_clr', 'dfp_flow_lkup_miss_sta_ctr_no_rd_clr',
            'flu_flow_table_hit_ctr_no_rd_clr', 'flu_flow_table_miss_ctr_no_rd_clr',
            'cfp_flow_lkup_flow_hit_stats_ctr_no_rd_clr', 'cfp_flow_lkup_miss_stats_ctr_no_rd_clr',
            'tmi_tx_fifo0_pkt_stats_ctr_no_rd_clr', 'tmi_rx_fp_pkt_stats_ctr_no_rd_clr')


def lif_entry(port, vlan, zone, miss_next_hop, port_mask=63, vlan_mask=4095):
    """Same layout as the C encoder (ffn_fe100_lif_encode): flags, zone|miss hop, port, 80-bit mask, 80-bit key."""
    out = bytearray(36)
    struct.pack_into('>III', out, 4, 0x80040000, (zone << 16) | miss_next_hop, port << 16)
    out[16:26] = ((vlan_mask << 38) | (port_mask << 32)).to_bytes(10, 'big')
    out[26:36] = ((vlan << 38) | (port << 32)).to_bytes(10, 'big')
    return bytes(out)


def lef_entry(port):
    return struct.pack('>IIH', 0x80000000 | (port << 16), 0, 0)


def qmap_entry(flow, ingress, queue, hop):
    """The lab's IPv4 queue map: (ingress logical port, next-hop index, post-NAT addresses) -> egress BCM queue.

    The third word is TCAM-matched (mask 0xffff at bytes 24..28); every lab run used 31 because its
    next hop was 31. The first production run (2026-10-19, flow ids 0x6000/0x6001) kept 31 against
    next hops 20/21 and every hit raised egress exception 17.
    """
    if type(queue) is not int or not 0 <= queue <= 65535:
        raise ValueError('invalid egress queue')
    qm = bytearray(84)
    struct.pack_into('>III', qm, 0, 0x02020000 | queue, (ingress << 6) | 1, hop)
    qm[12:20] = output_key4(flow)[8:16]
    struct.pack_into('>II', qm, 20, 0xfc0, 0xffff)
    qm[28:36] = b'\xff' * 8
    return bytes(qm)


def acl_entry(key):
    acl = bytearray(90)
    struct.pack_into('>I', acl, 0, 1 << 15)
    acl[4:20] = key
    acl[21:37] = b'\0' + b'\xff' * 15
    return bytes(acl)


# ---------------------------------------------------------------- native worker (one table block per process)
def worker(request, fd, plan):
    from ffn_fe100 import Fe100, bar0_base_and_size, memory_decode_on
    kind = request['kind']
    if kind == 'readiness':
        from ffn_fe100_live_sessions import LiveSessions
        if 'readiness' not in WORKER_STATE:
            WORKER_STATE['readiness'] = LiveSessions(False, lock_fd=fd, commissioning=True)
        return WORKER_STATE['readiness'].status()
    if kind == 'session':
        from ffn_fe100_live_sessions import LiveSessions
        if 'live' not in WORKER_STATE:
            WORKER_STATE['live'] = LiveSessions(True, lock_fd=fd, commissioning=True)
        live = WORKER_STATE['live']
        data = bytes.fromhex(request['data'])
        keys = tuple(bytes.fromhex(k) for k in plan['session_keys'])
        allowed = tuple(bytes.fromhex(e) for e in plan['session_entries'])
        learned = (len(data) == 64 and data[:16] in keys and data == entry4(data[:16], int.from_bytes(data[36:40], 'big')))
        if data not in allowed and not (learned and request['op'] == 'delete'):
            raise ValueError('unrecognized session entry for this commissioning run')
        rc, native = live.call(request['op'], encode_native(data))
        return {'rc': rc, 'data': decode_native(native, data[:16]).hex() if rc == 0 else data.hex(), 'native': native.hex()}
    if kind == 'snapshot':
        fe = Fe100()
        try:
            blocks = ('nif', 'lif', 'acl', 'dfp', 'flu', 'cfp', 'fwd', 'lef', 'egr', 'ipq', 'tmi', 'prw', 'sem', 'par', 'qmm', 'lag', 'tlu')
            regs = {r['name']: r['addr'] for r in json.loads(Path(register_map_path()).read_text())
                    if r['name'].endswith('_no_rd_clr') and r['name'].split('_', 1)[0] in blocks}
            return {name: fe.read32(addr) for name, addr in regs.items()}
        finally:
            fe.close()
    specs = {'acl': (0x80000, 90, 'fetch_acl_entry', 'insert_acl_entry'),
             'txport': (0x10000, 3, 'get_tx_portmap_entry', 'set_tx_portmap_entry'),
             'rxport': (0x10000, 1, 'get_rx_portmap_entry', 'set_rx_portmap_entry')}
    base, size, getname, putname = specs[kind]
    index = request['index']
    if index not in plan['pools'][kind]:
        raise ValueError('outside this run\'s reserved indices: %s %s' % (kind, index))
    if 'lib' not in WORKER_STATE and hashlib.sha256(Path(LIB).read_bytes()).hexdigest() != SHA:
        raise RuntimeError('owner ABI changed')
    address, span = bar0_base_and_size()
    if span != 0x100000 or not memory_decode_on():
        raise RuntimeError('BAR unavailable')
    if 'lib' not in WORKER_STATE:
        shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-flow-memory.so', mode=os.RTLD_GLOBAL | os.RTLD_NOW)
        if shim.ffn_fe100_select_block(base) or (base == 0x80000 and shim.ffn_fe100_select_lif_table(0)):
            raise RuntimeError('block selection failed')
        shim.ffn_fe100_open.argtypes = [C.c_uint64, C.c_char_p, C.c_int]
        trace = str(ROOT / ('first-session-io-' + str(time.time_ns()) + '.txt'))
        if shim.ffn_fe100_open(address, trace.encode(), 1):
            raise RuntimeError('map failed')
        for r in json.loads(Path(register_map_path()).read_text()):
            shim.ffn_fe100_allow(r['addr'])
        lib = C.CDLL(LIB, mode=os.RTLD_LOCAL | os.RTLD_LAZY)
        WORKER_STATE.update(lib=lib, shim=shim, trace=trace, kind=kind)
    if WORKER_STATE['kind'] != kind:
        raise RuntimeError('worker block changed')
    lib, shim, trace = (WORKER_STATE[k] for k in ('lib', 'shim', 'trace'))
    data = bytes.fromhex(request['data']) if 'data' in request else bytes(size)
    if len(data) != size:
        raise ValueError('incorrect table entry length')
    if kind == 'acl' and 'data' not in request:
        data = data[:4] + b'\x40' + data[5:]
    entry = (C.c_ubyte * size).from_buffer_copy(data)
    op = request['op']
    if kind == 'rxport':
        # Keyed by the switch (physical) port; the value is the FE100 logical port.
        key = (C.c_ubyte * 3)(0, 0, plan['rxport_physical'][str(index)])
        result = C.c_int(data[0])
        if op not in ('fetch', 'insert', 'delete'):
            raise ValueError('invalid RX map operation')
        fn = getattr(lib, 'pan_fe100_' + {'fetch': getname, 'insert': putname, 'delete': 'delete_rx_portmap_entry'}[op])
        fn.argtypes = [C.c_uint32, C.c_void_p] + ([] if op == 'delete' else [C.POINTER(C.c_int) if op == 'fetch' else C.c_int])
        args = (0, key) if op == 'delete' else (0, key, C.byref(result) if op == 'fetch' else result.value)
    elif op == 'delete':
        if kind not in ('acl', 'txport'):
            raise ValueError('delete not supported')
        fn = getattr(lib, 'pan_fe100_delete_' + ('tx_portmap' if kind == 'txport' else kind) + '_entry')
        fn.argtypes = [C.c_uint32] + [C.c_int] * (1 if kind == 'txport' else 2)
        args = (0, index) if kind == 'txport' else (0, 1, index)
    else:
        if op not in ('fetch', 'insert'):
            raise ValueError('invalid table operation')
        fn = getattr(lib, 'pan_fe100_' + (getname if op == 'fetch' else putname))
        fn.argtypes = [C.c_uint32, C.c_void_p, C.c_int]
        args = (0, entry, index)
    fn.restype = C.c_int
    shim.ffn_flow_watchdog(10)
    try:
        rc = fn(*args)
    finally:
        shim.ffn_flow_watchdog(0)
    if shim.ffn_fe100_faults():
        raise RuntimeError('register scope violation; inspect ' + trace)
    if kind == 'rxport':
        entry = bytes((result.value,))
    return {'rc': rc, 'data': bytes(entry).hex(), 'trace': trace}


# ---------------------------------------------------------------- controller
class Run:
    def __init__(self):
        self.lock = open('/run/ffn-fe100-tables.lock', 'a')
        deadline = time.monotonic() + 60   # the CP owner's periodic reconcile takes this lock briefly
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.lock.close(); raise RuntimeError('FE100 table lock busy for 60 s')
                time.sleep(0.5)
            except BaseException:
                self.lock.close(); raise
        self.workers = {}; self.resources = None; self.plan = None; self.entries = None
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.path = ROOT / ('first-session-' + str(time.time_ns()) + '.json')
        self.record = {'schema': 1, 'owner_sha256': SHA, 'cp_boot_id': boot, 'changes': [], 'snapshots': {},
                       'stage': 'preflight', 'session_touched': False, 'plan': None}
        self.save()

    def save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(self.record, f); f.flush(); os.fsync(f.fileno())
        os.replace(temp, self.path)

    def close(self):
        if self.resources is not None:
            self.resources.close(); self.resources = None
        for process, err in self.workers.values():
            try: process.stdin.close()
            except BrokenPipeError: pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
            process.stdout.close(); err.close()
        self.workers.clear(); self.lock.close()

    def call(self, kind, op='fetch', index=None, data=None):
        if kind in ('smac', 'nexthop', 'lif', 'lef', 'qm'):
            from ffn_fe100_resource_tables import ResourceTables, SPECS
            if self.resources is None:
                pools = self.plan['pools']
                self.resources = ResourceTables(dict(smac=pools['smac'], nexthop=pools['nexthop'], lif=pools['lif'],
                                                     lef=pools['lef'], qmap4=pools['qm']), lock_fd=self.lock.fileno())
            table = 'qmap4' if kind == 'qm' else kind
            raw = (bytes.fromhex(data) if isinstance(data, str) else data) if op == 'insert' else None
            result = self.resources.call(table, op, index, raw)
            return dict(rc=3 if result is None else 0, data=(result if result is not None else bytes(SPECS[table])).hex())
        req = dict(kind=kind, op=op)
        if index is not None: req['index'] = index
        if data is not None: req['data'] = data.hex() if isinstance(data, bytes) else data
        if kind in self.workers and self.workers[kind][0].poll() is not None:
            if op != 'fetch': raise RuntimeError('worker exited; reconcile by fetch before mutation')
            old, err = self.workers.pop(kind)
            try: old.stdin.close()
            except BrokenPipeError: pass
            old.stdout.close(); err.close()
        if kind not in self.workers:
            import tempfile
            err = tempfile.TemporaryFile(mode='w+')
            process = subprocess.Popen([sys.executable, __file__, '--worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=err, text=True, pass_fds=(self.lock.fileno(),),
                                       env={**os.environ, 'FFN_FE100_LOCK_FD': str(self.lock.fileno()), 'FFN_FIRST_SESSION_PLAN': json.dumps(self.plan or {})})
            self.workers[kind] = (process, err)
        process, err = self.workers[kind]
        process.stdin.write(json.dumps(req) + '\n'); process.stdin.flush()
        if not select.select([process.stdout], [], [], 25)[0]:
            process.kill(); process.wait(); raise TimeoutError('worker timeout: ' + kind)
        line = process.stdout.readline()
        if not line:
            err.seek(0); raise RuntimeError('worker failed: ' + err.read()[-3000:])
        result = json.loads(line)
        if result.get('rc', 0) not in ((0, 3) if op == 'fetch' else (0,)):
            raise RuntimeError('%s %s: %s' % (kind, op, result))
        return result

    @staticmethod
    def payload(kind, data):
        raw = bytes.fromhex(data)
        if kind == 'qm':
            raw = bytearray(raw[:36]); raw[0] &= 3; raw[4] &= 0x7f; raw[7] &= 0xfc; raw[20] &= 0x7f; raw[23] &= 0xfc
            return bytes(raw)
        if kind == 'acl':
            raw = bytearray(raw[:38]); raw[21] &= 0x3f
            return bytes(raw)
        return raw[1:] if kind == 'nexthop' else raw

    def write(self, kind, index, wanted):
        before = self.call(kind, index=index)
        change = {'kind': kind, 'index': index, 'before': before, 'wanted': wanted.hex(), 'restored': False}
        self.record['changes'].append(change); self.save()
        self.call(kind, 'insert', index, wanted)
        after = self.call(kind, index=index)
        if after['rc'] or self.payload(kind, after['data']) != self.payload(kind, wanted.hex()):
            raise RuntimeError('table readback mismatch: ' + kind)

    def ensure(self, kind, index, wanted):
        """Port-map entries may legitimately pre-exist with the wanted value."""
        current = self.call(kind, index=index)
        if current['rc'] == 0:
            if current['data'] != wanted.hex():
                raise RuntimeError('%s[%s] is owned with different content: %s' % (kind, index, current['data']))
            self.record.setdefault('preexisting', []).append(dict(kind=kind, index=index, data=current['data'])); self.save()
            return
        self.write(kind, index, wanted)

    # ---- plan -> entries
    def prepare(self, plan):
        if self.plan is not None: raise RuntimeError('already prepared')
        pools = plan['pools']
        s = plan['session']; lan = plan['lan']; wan = plan['wan']
        original = dict(s['original']); reply = dict(s['reply'])
        entries = session_pair4(int(s['id']), original, reply, (lan['zone'], wan['zone']), (pools['nexthop'][0], pools['nexthop'][1]))
        plan['session_entries'] = [e.hex() for e in entries]
        plan['session_keys'] = [e[:16].hex() for e in entries]
        plan['identities'] = [entry4(e[:16], int.from_bytes(e[36:40], 'big')).hex() for e in entries]
        plan['session_entries'] += plan['identities']
        self.plan = plan; self.record['plan'] = plan; self.save()
        self.entries = entries
        health = self.call('readiness')
        if health.get('commissioning_blockers'):
            raise RuntimeError('hardware prerequisites: ' + '; '.join(health['commissioning_blockers']))
        # preflight: our session keys must be absent, our resource slots empty
        for identity in plan['identities']:
            if self.call('session', data=bytes.fromhex(identity))['rc'] != 3:
                raise RuntimeError('session key already owned in hardware')
        for kind in ('nexthop', 'smac', 'lif', 'lef', 'qm'):
            for index in pools[kind]:
                if self.call(kind, index=index)['rc'] != 3:
                    raise RuntimeError('%s slot %d occupied' % (kind, index))
        for index in pools['acl']:
            if self.call('acl', index=index)['rc'] != 3:
                raise RuntimeError('acl slot %d occupied' % index)
        self.record['stage'] = 'preparing'; self.save()
        # port maps (identity-tolerant), LEFs, source MACs, next hops, LIFs, queue maps, ACLs
        for logical in pools['rxport']:
            self.ensure('rxport', logical, bytes((logical,)))
        self.ensure('txport', wan['logical'], bytes((0, 0, wan['physical'])))
        self.ensure('txport', lan['logical'], bytes((0, 0, lan['physical'])))
        lef_wan, lef_lan = pools['lef']
        self.write('lef', lef_wan, lef_entry(wan['logical']))
        self.write('lef', lef_lan, lef_entry(lan['logical']))
        smac_wan, smac_lan = pools['smac']
        self.write('smac', smac_wan, encode_smac(wan['smac']))
        self.write('smac', smac_lan, encode_smac(lan['smac']))
        nh_fwd, nh_rev, nh_miss = pools['nexthop']
        self.write('nexthop', nh_fwd, encode_front(lef_wan, dmac=plan['forward_dmac'], mtu=plan.get('mtu', 9216),
                                                   vlan=wan['vlan'] or None, smac_index=smac_wan))
        self.write('nexthop', nh_rev, encode_front(lef_lan, dmac=plan['reverse_dmac'], mtu=plan.get('mtu', 9216),
                                                   vlan=lan['vlan'] or None, smac_index=smac_lan))
        self.write('nexthop', nh_miss, next_hop(destination=plan.get('miss_sysport', 8), dmac=plan.get('miss_dmac', '02:52:20:ab:cd:ee')))
        lif_lan, lif_wan = pools['lif']
        ingress = plan['ingress_pport']
        self.write('lif', lif_lan, lif_entry(ingress, lan['vlan'], lan['zone'], nh_miss, port_mask=plan.get('pport_mask', 63)))
        self.write('lif', lif_wan, lif_entry(ingress, wan['vlan'], wan['zone'], nh_miss, port_mask=plan.get('pport_mask', 63)))
        qm_fwd, qm_rev = pools['qm']
        self.write('qm', qm_fwd, qmap_entry(entries[0], ingress, plan['queues'][str(wan['physical'])], nh_fwd))
        self.write('qm', qm_rev, qmap_entry(entries[1], ingress, plan['queues'][str(lan['physical'])], nh_rev))
        acl_fwd, acl_rev = pools['acl']
        self.write('acl', acl_fwd, acl_entry(entries[0][:16]))
        self.write('acl', acl_rev, acl_entry(entries[1][:16]))
        self.record['stage'] = 'prepared'; self.save()
        return dict(entries=plan['session_entries'][:2], keys=plan['session_keys'])

    def install(self):
        if self.entries is None: raise RuntimeError('prepare first')
        self.record['session_touched'] = True; self.save()
        for wanted in self.entries:
            self.remove_one(wanted)
            self.call('session', 'insert', data=wanted)
            self.call('session', 'update', data=wanted)
            actual = self.call('session', data=wanted)
            if actual['rc'] or actual['data'] != wanted.hex():
                raise RuntimeError('session readback mismatch: ' + str(actual))
        self.record['stage'] = 'installed'; self.save()

    def remove_one(self, wanted):
        identity = entry4(wanted[:16], int.from_bytes(wanted[36:40], 'big'))
        current = self.call('session', data=identity)
        if current['rc'] == 3: return
        data = bytes.fromhex(current['data'])
        if data != wanted and data != entry4(wanted[:16], int.from_bytes(data[36:40], 'big')):
            raise RuntimeError('session ownership conflict: ' + current['data'])
        self.call('session', 'delete', data=current['data'])
        if self.call('session', data=identity)['rc'] != 3:
            raise RuntimeError('session delete not verified')

    def remove(self):
        if self.entries is None: return
        for wanted in self.entries:
            self.remove_one(wanted)

    def restore(self):
        errors = []
        if self.record.get('session_touched'):
            try: self.remove()
            except Exception as e: errors.append('sessions: ' + str(e))
        if errors:
            self.record['cleanup_errors'] = errors; self.record['stage'] = 'recovery_required'; self.save()
            raise RuntimeError('; '.join(errors))
        for c in reversed(self.record['changes']):
            if c['restored']: continue
            try:
                now = self.call(c['kind'], index=c['index']); before = c['before']
                if now['rc'] == before['rc'] and self.payload(c['kind'], now['data']) == self.payload(c['kind'], before['data']):
                    c['restored'] = True; self.save(); continue
                if now['rc'] or self.payload(c['kind'], now['data']) != self.payload(c['kind'], c['wanted']):
                    raise RuntimeError('ownership conflict: ' + c['kind'])
                self.call(c['kind'], 'delete' if before['rc'] == 3 else 'insert', c['index'], before['data'])
                rb = self.call(c['kind'], index=c['index'])
                if rb['rc'] != before['rc'] or (rb['rc'] == 0 and self.payload(c['kind'], rb['data']) != self.payload(c['kind'], before['data'])):
                    raise RuntimeError('restore mismatch: ' + c['kind'])
                c['restored'] = True; self.save()
            except Exception as e:
                errors.append(str(e))
        self.record['cleanup_errors'] = errors; self.record['stage'] = 'restored' if not errors else 'recovery_required'; self.save()
        if errors: raise RuntimeError('; '.join(errors))

    def command(self, message):
        op = message['op']
        if op == 'prepare': result = self.prepare(message['plan'])
        elif op == 'install': self.install(); result = {}
        elif op == 'remove': self.remove(); result = {}
        elif op == 'snapshot': result = {}
        else: raise ValueError('unknown command')
        snapshot = self.call('snapshot')
        self.record['snapshots'][op + '-' + str(time.time_ns())] = snapshot; self.save()
        return dict(ok=True, operation=op, journal=str(self.path), registers=snapshot, **({'result': result} if result else {}))


def main():
    if sys.argv[1:] == ['--worker']:
        fd = int(os.environ['FFN_FE100_LOCK_FD'])
        if os.readlink('/proc/self/fd/' + str(fd)) != '/run/ffn-fe100-tables.lock':
            raise RuntimeError('missing lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads(os.environ.get('FFN_FIRST_SESSION_PLAN') or '{}')
        while select.select([sys.stdin], [], [], 600)[0]:
            line = sys.stdin.readline()
            if not line: break
            request = json.loads(line)
            if 'plan' in request: plan = request['plan']; print(json.dumps({'rc': 0, 'plan': True}), flush=True); continue
            print(json.dumps(worker(request, fd, plan)), flush=True)
        return
    if sys.argv[1:] != ['--serve']:
        raise SystemExit('use --serve')
    run = Run()
    try:
        print(json.dumps({'ready': True, 'journal': str(run.path)}), flush=True)
        while select.select([sys.stdin], [], [], 120)[0]:
            line = sys.stdin.readline()
            if not line: break
            message = json.loads(line)
            if message.get('op') == 'finish': break
            try:
                print(json.dumps(run.command(message)), flush=True)
            except Exception as error:
                print(json.dumps({'ok': False, 'error': str(error)[:2000], 'journal': str(run.path)}), flush=True)
    finally:
        try:
            run.restore(); restored = True; error = None
        except Exception as e:
            restored = False; error = str(e)
        print(json.dumps({'restored': restored, 'error': error, 'journal': str(run.path)}), flush=True)
        run.close()


if __name__ == '__main__':
    main()
