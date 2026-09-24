#!/usr/bin/env python3
"""Bounded CP physical-session commissioning; no permanent activation.

--serve accepts prepare/install/drop/remove/snapshot/finish JSON lines while
holding the table lock. EOF or 60 seconds without a command restores owned
entries. Each native worker has a ten-second watchdog. A durable journal
records intent before writes; a failed restore is never marked successful.
BCM routing and packet injection are controlled separately by the MP tester.

Reference: VM 5220-sysroot1-full libpandp_cp.so DWARF, usr/share/pdt/fe100.py,
opt/dpfs/etc/fe-parser.json. IPv4 UDP test key, LIF1, ACL31 and NH31 only.
"""
from ffn_fe100 import register_map_path
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import time
from ffn_fe100_sessions import key4, entry4, forwarding_entry4, nat_entry4, output_key4
from ffn_fe100_session_adapter import encode_native, decode_native
from ffn_fe100_nexthop import LIB, SHA, encode as next_hop

FRONT_RETURN=int(os.environ.get('FFN_FE100_FRONT_RETURN','13'))
if FRONT_RETURN not in (5,13):raise ValueError('unsupported front return')
EGRESS=(18-FRONT_RETURN) if os.environ.get('FFN_FE100_CROSS')=='1' else FRONT_RETURN
VLAN_RETURN=os.environ.get('FFN_FE100_VLAN_RETURN')=='1'
NAT_MODE=os.environ.get('FFN_FE100_NAT_LAB')
PROTOCOL={'udp':17,'tcp':6}[os.environ.get('FFN_FE100_LAB_PROTOCOL','udp')]
LAB_LIF=2 if FRONT_RETURN==5 else 1
KEY = (key4('198.18.0.2','198.18.0.1',49001,49000,PROTOCOL,4094) if FRONT_RETURN==5 else
       key4('198.18.0.1', '198.18.0.2', 49000, 49001, PROTOCOL, 4094))
if NAT_MODE:
    if not VLAN_RETURN or EGRESS==FRONT_RETURN:raise ValueError('NAT lab requires isolated cross-port VLAN return')
    from ffn_fe100_nat_lab import tuples
    ORIGINAL,TRANSLATED=tuples(NAT_MODE,FRONT_RETURN==5)
    KEY=key4(ORIGINAL['source'],ORIGINAL['destination'],ORIGINAL['source_port'],ORIGINAL['destination_port'],PROTOCOL,4094)
IDENTITY = entry4(KEY, 1001)
FORWARD = (nat_entry4(KEY,1001,31,TRANSLATED) if NAT_MODE else
           forwarding_entry4(KEY, 1001, 31,decrement_ttl=VLAN_RETURN))
RETURN_KEY=output_key4(FORWARD)
RETURN_KEY=RETURN_KEY[:2]+(4093).to_bytes(2,'big')+RETURN_KEY[4:]
RETURN_IDENTITY=entry4(RETURN_KEY,1002)
DROP = forwarding_entry4(KEY, 1001, drop=True)
ROOT = Path('/var/lib/ffn/fe100')
WORKER_STATE = {}


def front_qmap(flow,ingress,queue):
    if type(queue)!=int or not 0<=queue<=65535:raise ValueError('Invalid observed egress queue')
    qm=bytearray(84)
    struct.pack_into('>III',qm,0,0x02020000|queue,(ingress<<6)|1,31)
    # QMAP matches post-NAT addresses. Original-address matching produced
    # egress exception17; translated addresses passed physical address-NAT
    # tests in both directions. Queue IDs come from current BCM allocation.
    qm[12:20]=output_key4(flow)[8:16]
    struct.pack_into('>II',qm,20,0xfc0,0xffff)
    qm[28:36]=b'\xff'*8
    return bytes(qm)


def worker(request, fd):
    from ffn_fe100 import Fe100, bar0_base_and_size, memory_decode_on
    kind = request['kind']
    if kind=='readiness':
        from ffn_fe100_live_sessions import LiveSessions
        return LiveSessions(False,lock_fd=fd,commissioning=True).status()
    if kind == 'session':
        from ffn_fe100_live_sessions import LiveSessions
        if 'live' not in WORKER_STATE: WORKER_STATE['live'] = LiveSessions(True, lock_fd=fd,commissioning=True)
        live = WORKER_STATE['live']
        data = bytes.fromhex(request.get('data', IDENTITY.hex()))
        # A first physical packet can create an identity entry with an ASIC
        # allocated flow ID. Allow exact-key deletion of that learned entry;
        # only the controller's preflight/ownership journal may request it.
        learned = (len(data)==64 and data[:16] in (KEY,RETURN_KEY) and
                   data==entry4(data[:16],int.from_bytes(data[36:40],'big')))
        if data not in (IDENTITY, RETURN_IDENTITY, FORWARD, DROP) and not (learned and request['op']=='delete'):
            raise ValueError('unrecognized lab session')
        rc, native = live.call(request['op'], encode_native(data))
        return {'rc':rc, 'data':decode_native(native, data[:16]).hex() if rc == 0 else data.hex(),
                'native':native.hex()}
    if kind == 'snapshot':
        from ffn_fe100_lookup_health import selected, decode
        fe = Fe100()
        try:
            regs = json.loads(Path(register_map_path()).read_text())
            return {r['name']:{'raw':v, 'fields':decode(r,v)} for r in regs
                    if selected(r['name']) or (r['name'].split('_')[0] in ('flu','cfp','nif','prw','tmi')
                                              and r['name'].endswith('_no_rd_clr'))
                    for v in [fe.read32(r['addr'])]}
        finally:
            fe.close()
    specs = {'parser':(0x20000,32,'parser_entry_fetch','parser_entry_insert'),
             'lif':(0x80000,36,'fetch_lif_entry','insert_lif_entry'),
             'acl':(0x80000,90,'fetch_acl_entry','insert_acl_entry'),
             'qm':(0x80000,84,'fetch_qm_entry','insert_qm_entry'),
             'spm':(0x70000,2,'fetch_spm_entry','set_spm_entry'),
             'lef':(0x58000,10,'fetch_lef_entry','insert_lef_entry'),
             'txport':(0x10000,3,'get_tx_portmap_entry','set_tx_portmap_entry'),
             'nexthop':(0x50000,16,'fetch_nexthop_entry','insert_nexthop_entry')}
    base, size, getname, putname = specs[kind]
    index = request['index']
    if index not in (range(55) if kind == 'parser' else (5,13) if kind=='txport' else (52,53,54,55) if kind=='spm' else (1,2,31) if kind == 'lif' else (30,31)):
        raise ValueError('outside reserved lab indices')
    if 'lib' not in WORKER_STATE and hashlib.sha256(Path(LIB).read_bytes()).hexdigest() != SHA:
        raise RuntimeError('owner ABI changed')
    address, span = bar0_base_and_size()
    if span != 0x100000 or not memory_decode_on(): raise RuntimeError('BAR unavailable')
    if 'lib' not in WORKER_STATE:
        shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-flow-memory.so', mode=os.RTLD_GLOBAL|os.RTLD_NOW)
        if shim.ffn_fe100_select_block(base) or (base == 0x80000 and shim.ffn_fe100_select_lif_table(0)):
            raise RuntimeError('block selection failed')
        shim.ffn_fe100_open.argtypes = [C.c_uint64,C.c_char_p,C.c_int]
        trace = str(ROOT/('packet-io-'+str(time.time_ns())+'.txt'))
        if shim.ffn_fe100_open(address,trace.encode(),1): raise RuntimeError('map failed')
        # Selected block only; native IA APIs need capability/command/data CSRs.
        for r in json.loads(Path(register_map_path()).read_text()):
            shim.ffn_fe100_allow(r['addr'])
        lib = C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
        WORKER_STATE.update(lib=lib,shim=shim,trace=trace,kind=kind)
    if WORKER_STATE['kind'] != kind: raise RuntimeError('worker block changed')
    lib,shim,trace=(WORKER_STATE[k] for k in ('lib','shim','trace'))
    data = bytes.fromhex(request['data']) if 'data' in request else bytes(size)
    if kind=='spm' and 'data' not in request:
        data=(index<<4).to_bytes(2,'big')
    if len(data) != size: raise ValueError('incorrect table entry length')
    if kind == 'acl' and 'data' not in request:
        data = data[:4]+b'\x40'+data[5:]
    if kind=='qm' and 'data' not in request:data=data[:7]+b'\x01'+data[8:]
    entry = (C.c_ubyte*size).from_buffer_copy(data)
    op = request['op']
    if op == 'delete':
        if kind not in ('acl','qm','nexthop','lef','txport','lif'): raise ValueError('delete not supported')
        fn = getattr(lib,'pan_fe100_delete_'+('tx_portmap' if kind=='txport' else kind)+'_entry')
        fn.argtypes = [C.c_uint32]+[C.c_int]*(1 if kind in ('lef','txport','lif') else 2)
        args = (0,index) if kind in ('lef','txport','lif') else (0,1 if kind in ('acl','qm') else 0,index)
    else:
        if op not in ('fetch','insert'): raise ValueError('invalid table operation')
        fn = getattr(lib,'pan_fe100_'+(getname if op == 'fetch' else putname))
        fn.argtypes = [C.c_uint32,C.c_void_p]+([C.c_int]* (2 if kind == 'nexthop' else 0 if kind=='spm' else 1))
        args = (0,entry,0,index) if kind == 'nexthop' else (0,entry) if kind=='spm' else (0,entry,index)
    fn.restype = C.c_int
    shim.ffn_flow_watchdog(10)
    try: rc = fn(*args)
    finally: shim.ffn_flow_watchdog(0)
    if shim.ffn_fe100_faults(): raise RuntimeError('register scope violation; inspect '+trace)
    return {'rc':rc,'data':bytes(entry).hex(),'trace':trace}


class Lab:
    def __init__(self):
        self.lock = open('/run/ffn-fe100-tables.lock','a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.path = ROOT/('packet-session-'+str(time.time_ns())+'.json')
        self.record = {'schema':1,'owner_sha256':SHA,
                       'profile':{'ingress':FRONT_RETURN,'egress':EGRESS,'vlan_return':VLAN_RETURN,'nat_mode':NAT_MODE,
                                  'session_key':KEY.hex(),'return_key':RETURN_KEY.hex()},
                       'cp_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                       'changes':[], 'snapshots':{}, 'stage':'preflight', 'session_offload_verified':False}
        self.prepared = False
        self.workers = {}
        self.save()

    def save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(self.record,f); f.flush(); os.fsync(f.fileno())
        os.replace(temp,self.path)

    def call(self, kind, op='fetch', index=31, data=None):
        req = dict(kind=kind,op=op,index=index)
        if data is not None: req['data'] = data.hex() if isinstance(data,bytes) else data
        if kind in self.workers and self.workers[kind][0].poll() is not None:
            if op!='fetch':raise RuntimeError('worker exited; reconcile by fetch before mutation')
            old,err=self.workers.pop(kind)
            try:old.stdin.close()
            except BrokenPipeError:pass
            old.stdout.close();err.close()
        if kind not in self.workers:
            import tempfile
            err=tempfile.TemporaryFile(mode='w+')
            process=subprocess.Popen([sys.executable,__file__,'--worker'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,
                pass_fds=(self.lock.fileno(),),
                env={**os.environ,'FFN_FE100_LOCK_FD':str(self.lock.fileno())})
            self.workers[kind]=(process,err)
        process,err=self.workers[kind]
        process.stdin.write(json.dumps(req)+'\n');process.stdin.flush()
        if not select.select([process.stdout],[],[],20)[0]:
            process.kill();process.wait();raise TimeoutError('packet worker timeout: '+kind)
        line=process.stdout.readline()
        if not line:
            err.seek(0);raise RuntimeError('packet worker failed: '+err.read()[-3000:])
        result=json.loads(line)
        if result.get('rc',0) not in ((0,3) if op == 'fetch' else (0,)):
            raise RuntimeError(f'{kind} {op}: {result}')
        return result

    @staticmethod
    def payload(kind, data):
        raw = bytes.fromhex(data)
        if kind=='qm':
            raw=bytearray(raw[:36]);raw[0]&=3
            # Native TCAM key valid bits and table selector are generated.
            raw[4]&=0x7f;raw[7]&=0xfc;raw[20]&=0x7f;raw[23]&=0xfc
            return bytes(raw)
        # Hardware-generated next-hop ECC and ACL hit counter are not policy.
        if kind == 'acl':
            # Native ACL API selects a separate IPv4/IPv6 table using key.pt;
            # the type mask is not stored in the TCAM key (pdt _insert_fe100).
            raw = bytearray(raw[:38]); raw[21] &= 0x3f
            return bytes(raw)
        return raw[1:] if kind == 'nexthop' else raw

    def write(self,kind,index,wanted):
        before = self.call(kind,index=index)
        change = {'kind':kind,'index':index,'before':before,'wanted':wanted.hex(),'restored':False}
        self.record['changes'].append(change); self.save()
        self.call(kind,'insert',index,wanted)
        after = self.call(kind,index=index)
        if after['rc'] or self.payload(kind,after['data']) != self.payload(kind,wanted.hex()):
            raise RuntimeError('table readback mismatch: '+kind)

    def prepare(self):
        if self.prepared or self.record['changes']: raise RuntimeError('already prepared')
        if self.call('session')['rc'] != 3: raise RuntimeError('lab flow already owned')
        for index in (30,31):
            if self.call('nexthop',index=index)['rc'] != 3: raise RuntimeError('lab next-hop occupied')
        if self.call('acl')['rc'] != 3: raise RuntimeError('lab ACL occupied')
        if VLAN_RETURN:
            if EGRESS==FRONT_RETURN:raise RuntimeError('VLAN return requires distinct ports')
            if self.call('lif',index=31)['rc']!=3:raise RuntimeError('return LIF31 occupied')
            if self.call('session',data=RETURN_IDENTITY)['rc']!=3:raise RuntimeError('return flow already owned')
        lif = self.call('lif',index=LAB_LIF)
        expected = bytearray(36)
        struct.pack_into('>III',expected,4,0x80050000,8,FRONT_RETURN<<16)
        expected[16:26]=(63<<32).to_bytes(10,'big');expected[26:36]=(FRONT_RETURN<<32).to_bytes(10,'big')
        if lif['rc'] or bytes.fromhex(lif['data'])[4:] != expected[4:]:
            raise RuntimeError('ingress LIF differs from front-port baseline')
        from ffn_fe100_parser_apply import SOURCE, encode
        source = SOURCE.read_bytes()
        if hashlib.sha256(source).hexdigest() != '6dcbd4fa1e12e5798a55bf22dded9f3fdfc5d0ee90d454d8a4ee88238a1bbddf':
            raise RuntimeError('parser source changed')
        table = json.loads(source)['pan_fe100_parse_table']
        # Parsed traffic can learn a session before the explicit install.
        # Record ownership after absent-key preflight, before enabling parsing,
        # so an interrupted prepare/miss phase also removes learned entries.
        self.prepared=True;self.record['session_touched']=True;self.save()
        # ACL allow is an exact 5-tuple+zone match in the native IPv4 table.
        acl = bytearray(90);struct.pack_into('>I',acl,0,1<<15)
        acl[4:20]=KEY;acl[21:37]=b'\0'+b'\xff'*15
        self.write('acl',31,bytes(acl))
        front_return='FFN_FE100_FRONT_RETURN' in os.environ
        if front_return:
            from ffn_fe100_nexthop import encode_front
            if self.call('lef')['rc']!=3:raise RuntimeError('LEF31 occupied')
            if self.call('qm')['rc']!=3:raise RuntimeError('QMAP31 occupied')
            physical=7 if EGRESS==13 else 16
            mapping=bytes((0,0,physical))
            tx=self.call('txport',index=EGRESS)
            if tx['rc']!=3 and tx['data']!=mapping.hex():raise RuntimeError('TX port mapping conflict')
            if tx['rc']==3:self.write('txport',EGRESS,mapping)
            # XF removes the CPU message header and emits a DSA-tagged frame
            # through NIF. The scoped BCM rule selects RAW_DSA front egress.
            from ffn_fe100_bcm_lab import run
            queues=run({'mode':'queue-status'})['queue_ids']
            self.record['bcm_queue_ids']=queues;self.save()
            self.write('qm',31,front_qmap(FORWARD,FRONT_RETURN,queues[physical]))
            self.write('lef',31,struct.pack('>IIH',0x80000000|(EGRESS<<16),0,0))
            wanted=encode_front(31,dmac='02:52:20:ab:cd:ee',vlan=4000 if VLAN_RETURN else None)
        else:wanted=next_hop(destination=8,dmac='02:52:20:ab:cd:ee')
        self.write('nexthop',31,wanted)
        struct.pack_into('>II',expected,4,0x80040000,(4094<<16)|30)
        if VLAN_RETURN:
            # Packed owner DWARF: VID at key bits49:38; pport at37:32.
            expected[16:26]=((4095<<38)|(63<<32)).to_bytes(10,'big')
            capture=bytearray(expected)
            struct.pack_into('>II',capture,4,0x80050000,(4093<<16)|8)
            capture[26:36]=((4000<<38)|(FRONT_RETURN<<32)).to_bytes(10,'big')
            self.record['return_session_touched']=True;self.save()
            self.write('lif',31,bytes(capture))
        self.write('lif',LAB_LIF,bytes(expected))
        for index in range(55):
            wanted = encode(table[str(index)])
            before = self.call('parser',index=index)
            zero = '8000000080000000000000000000000000000000000000000000000000000001'
            if before['data'] not in (zero,wanted.hex()): raise RuntimeError('unexpected parser table')
            if before['data'] != wanted.hex(): self.write('parser',index,wanted)
        self.prepared=True;self.record['stage']='prepared';self.save()

    def remove_session(self,return_flow=False):
        key=RETURN_KEY if return_flow else KEY
        current = self.call('session',data=RETURN_IDENTITY if return_flow else IDENTITY)
        if current['rc'] == 3: return
        if current['data'] not in (FORWARD.hex(),DROP.hex()):
            data=bytes.fromhex(current['data'])
            if not self.prepared or data!=entry4(key,int.from_bytes(data[36:40],'big')):
                raise RuntimeError('session ownership conflict: '+current['data'])
            # The key was absent at preflight and only our reserved lab LIF
            # has this zone. Log the exact learned identity before deletion.
            self.record.setdefault('learned_entries',[]).append(current)
            self.record['session_touched']=True;self.save()
        self.call('session','delete',data=current['data'])
        if self.call('session',data=RETURN_IDENTITY if return_flow else IDENTITY)['rc'] != 3: raise RuntimeError('session delete not verified')

    def restore(self):
        errors=[]
        # Only remove a flow after our durable insert intent, never preflight conflicts.
        if self.record.get('session_touched'):
            try: self.remove_session()
            except Exception as e: errors.append(str(e))
        if self.record.get('return_session_touched'):
            try:self.remove_session(return_flow=True)
            except Exception as e:errors.append(str(e))
        for c in reversed(self.record['changes']):
            if c['restored']: continue
            try:
                now=self.call(c['kind'],index=c['index']);before=c['before']
                if now['rc']==before['rc'] and self.payload(c['kind'],now['data'])==self.payload(c['kind'],before['data']):
                    c['restored']=True;self.save();continue
                if now['rc'] or self.payload(c['kind'],now['data'])!=self.payload(c['kind'],c['wanted']):
                    raise RuntimeError('ownership conflict: '+c['kind'])
                self.call(c['kind'],'delete' if before['rc']==3 else 'insert',c['index'],before['data'])
                rb=self.call(c['kind'],index=c['index'])
                if rb['rc'] != before['rc'] or (rb['rc']==0 and self.payload(c['kind'],rb['data'])!=self.payload(c['kind'],before['data'])):
                    raise RuntimeError('restore mismatch: '+c['kind'])
                c['restored']=True;self.save()
            except Exception as e: errors.append(str(e))
        self.record['cleanup_errors']=errors;self.record['stage']='restored' if not errors else 'recovery_required';self.save()
        if errors: raise RuntimeError('; '.join(errors))

    def command(self, op):
        if op=='prepare': self.prepare()
        elif op in ('install','drop'):
            if not self.prepared: raise RuntimeError('prepare first')
            self.remove_session()
            self.record['session_touched']=True;self.save()
            wanted=FORWARD if op=='install' else DROP
            self.call('session','insert',data=wanted)
            self.call('session','update',data=wanted)
            actual=self.call('session')
            if actual['rc'] or actual['data']!=wanted.hex(): raise RuntimeError('session readback mismatch: '+str(actual))
        elif op=='remove':
            if self.record.get('session_touched'): self.remove_session()
        elif op!='snapshot': raise ValueError('unknown command')
        snapshot=self.call('snapshot')
        self.record['snapshots'][op+'-'+str(time.time_ns())]=snapshot;self.save()
        return {'ok':True,'operation':op,'journal':str(self.path),'registers':snapshot}


def main():
    if '--protocol' in sys.argv:
        index=sys.argv.index('--protocol')
        if (index!=len(sys.argv)-2 or sys.argv[index+1] not in ('udp','tcp') or
            not any(p in sys.argv[:index] for p in ('--front5','--front13'))):
            raise SystemExit('--protocol udp|tcp must be last')
        os.environ['FFN_FE100_LAB_PROTOCOL']=sys.argv[index+1]
        del sys.argv[index:]
    if '--nat' in sys.argv:
        index=sys.argv.index('--nat')
        if (index!=len(sys.argv)-2 or sys.argv[index+1] not in ('address','port') or
            sys.argv[1:index] not in (['--serve','--front13','--cross','--vlan-return'],
                                     ['--serve','--front5','--cross','--vlan-return'])):
            raise SystemExit('--nat address|port requires --serve --front5|--front13 --cross --vlan-return')
        os.environ['FFN_FE100_NAT_LAB']=sys.argv[index+1]
        del sys.argv[index:]
    if sys.argv[1:] in (['--serve','--front13','--cross','--vlan-return'],['--serve','--front5','--cross','--vlan-return']):
        os.environ['FFN_FE100_VLAN_RETURN']='1'
        sys.argv.remove('--vlan-return')
    if sys.argv[1:] in (['--serve','--front13','--cross'],['--serve','--front5','--cross']):
        os.environ['FFN_FE100_CROSS']='1'
        sys.argv.remove('--cross')
    if sys.argv[1:] in (['--serve','--front13'],['--serve','--front5']):
        os.environ['FFN_FE100_FRONT_RETURN']=sys.argv[2][7:]
        os.execv(sys.executable,[sys.executable,__file__,'--serve'])
    if sys.argv[1:]==['--worker']:
        fd=int(os.environ['FFN_FE100_LOCK_FD'])
        if os.readlink('/proc/self/fd/'+str(fd))!='/run/ffn-fe100-tables.lock': raise RuntimeError('missing lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Inherited lock must not outlive a lost controller indefinitely.
        while select.select([sys.stdin],[],[],600)[0]:
            line=sys.stdin.readline()
            if not line:break
            print(json.dumps(worker(json.loads(line),fd)),flush=True)
        return
    if sys.argv[1:]!=['--serve']: raise SystemExit('use --serve for isolated commissioning')
    lab=Lab()
    try:
        health=lab.call('readiness')
        if health['commissioning_blockers']:
            print(json.dumps({'ready':False,'blockers':health['commissioning_blockers'],
                              'journal':str(lab.path)}),flush=True)
            return
        print(json.dumps({'ready':True,'journal':str(lab.path),'hardware':health}),flush=True)
        while select.select([sys.stdin],[],[],60)[0]:
            line=sys.stdin.readline()
            if not line:break
            op=json.loads(line)['op']
            if op=='finish':break
            print(json.dumps(lab.command(op)),flush=True)
    finally:
        try:
            lab.restore()
            print(json.dumps({'restored':True,'journal':str(lab.path)}),flush=True)
        finally:
            for process,err in lab.workers.values():
                try:process.stdin.close()
                except BrokenPipeError:pass
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                err.close()


if __name__=='__main__':main()
