#!/usr/bin/env python3
"""Bounded CP physical-session commissioning; no permanent activation.

--serve accepts prepare/install/drop/remove/snapshot/finish JSON lines while
holding the table lock. EOF or 60 seconds without a command restores owned
entries. Each native worker has a ten-second watchdog. A durable journal
records intent before writes; a failed restore is never marked successful.
BCM routing and packet injection are controlled separately by the MP tester.

Reference: VM 5220-sysroot1-full libpandp_cp.so DWARF, usr/share/pdt/fe100.py,
opt/dpfs/etc/fe-parser.json. Reserved benchmark keys, profile-selected lab LIFs,
ACL31 and NH31 only. Environment port selection requires an isolated supervisor.
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

from ffn_fe100_lab_ports import profile
PORT_PROFILE=profile()
PORT_PAIR=PORT_PROFILE['front']
FRONT_RETURN=int(os.environ.get('FFN_FE100_FRONT_RETURN',str(PORT_PAIR[1])))
if FRONT_RETURN not in PORT_PAIR:raise ValueError('unsupported front return')
EGRESS=next(p for p in PORT_PAIR if p!=FRONT_RETURN) if os.environ.get('FFN_FE100_CROSS')=='1' else FRONT_RETURN
if os.environ.get('FFN_FE100_MAC_SINGLE')=='1':EGRESS=FRONT_RETURN
VLAN_RETURN=os.environ.get('FFN_FE100_VLAN_RETURN')=='1'
ROUTED_LAB=os.environ.get('FFN_FE100_ROUTED_LAB')=='1'
MAC_LOOPBACK=os.environ.get('FFN_FE100_MAC_LOOPBACK')=='1'
SMAC_REWRITE=os.environ.get('FFN_FE100_SMAC_LAB')=='1'
PAIRED_NAT_LAB=os.environ.get('FFN_FE100_PAIRED_NAT_LAB')=='1'
IPV6_MISS_LAB=os.environ.get('FFN_FE100_IPV6_MISS_LAB')=='1'
IPV6_LAB_KEY=bytes.fromhex('80110ffebf68bf6920010db800000000000000000000000120010db8000000000000000000000002')
RETURN_PORT=EGRESS if MAC_LOOPBACK else FRONT_RETURN
NAT_MODE=os.environ.get('FFN_FE100_NAT_LAB')
PROTOCOL={'udp':17,'tcp':6}[os.environ.get('FFN_FE100_LAB_PROTOCOL','udp')]
LAB_LIF=PORT_PROFILE['lif'][FRONT_RETURN]
KEY = (key4('198.18.0.2','198.18.0.1',49001,49000,PROTOCOL,4094) if FRONT_RETURN==5 else
       key4('198.18.0.1', '198.18.0.2', 49000, 49001, PROTOCOL, 4094))
if NAT_MODE:
    if not (ROUTED_LAB and PORT_PAIR!=[5,13]) and (not VLAN_RETURN or (EGRESS==FRONT_RETURN and not MAC_LOOPBACK)):
        raise ValueError('NAT lab requires isolated cross-port or internal MAC VLAN return')
    from ffn_fe100_nat_lab import tuples
    ORIGINAL,TRANSLATED=tuples(NAT_MODE,(FRONT_RETURN==5)!=(os.environ.get('FFN_FE100_NAT_REVERSE')=='1'))
    KEY=key4(ORIGINAL['source'],ORIGINAL['destination'],ORIGINAL['source_port'],ORIGINAL['destination_port'],PROTOCOL,4094)
IDENTITY = entry4(KEY, 1001)
FORWARD = (nat_entry4(KEY,1001,31,TRANSLATED) if NAT_MODE else
           forwarding_entry4(KEY, 1001, 31,decrement_ttl=VLAN_RETURN or ROUTED_LAB))
RETURN_KEY=output_key4(FORWARD)
RETURN_KEY=RETURN_KEY[:2]+(4093).to_bytes(2,'big')+RETURN_KEY[4:]
RETURN_IDENTITY=entry4(RETURN_KEY,1002)
DROP = forwarding_entry4(KEY, 1001, drop=True)
REVERSE_IDENTITY=REVERSE_FORWARD=REVERSE_DROP=None
if PAIRED_NAT_LAB:
    if not NAT_MODE or PORT_PAIR==[5,13] or VLAN_RETURN or EGRESS!=FRONT_RETURN:
        raise ValueError('Paired NAT lab requires an isolated same-port optical return')
    reverse_original,reverse_translated=tuples(NAT_MODE,not ((FRONT_RETURN==5)!=(os.environ.get('FFN_FE100_NAT_REVERSE')=='1')))
    reverse_key=key4(reverse_original['source'],reverse_original['destination'],reverse_original['source_port'],reverse_original['destination_port'],PROTOCOL,4094)
    REVERSE_IDENTITY=entry4(reverse_key,1003)
    REVERSE_FORWARD=nat_entry4(reverse_key,1003,31,reverse_translated)
    REVERSE_DROP=forwarding_entry4(reverse_key,1003,drop=True)
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
    if kind=='session6':
        # Only fetch/delete of the isolated IPv6 miss key; no IPv6 action API.
        from ffn_fe100_live_sessions import LiveSessions
        if not IPV6_MISS_LAB or request['op'] not in ('fetch','delete'):
            raise ValueError('IPv6 miss recovery was not selected')
        if 'live' not in WORKER_STATE:WORKER_STATE['live']=LiveSessions(True,lock_fd=fd,commissioning=True)
        native=bytes(16)+IPV6_LAB_KEY+bytes(88)
        if request['op']=='delete':
            native=bytes.fromhex(request['data'])
            if (len(native)!=144 or native[16:56]!=IPV6_LAB_KEY or
                    native[56:76]!=bytes(20) or native[80:88]!=bytes(8)):
                raise ValueError('Not an owned plain IPv6 identity')
        rc,result=WORKER_STATE['live'].call(request['op'],native)
        return {'rc':rc,'data':result.hex()}
    if kind=='readiness':
        from ffn_fe100_live_sessions import LiveSessions
        if 'readiness' not in WORKER_STATE:
            WORKER_STATE['readiness']=LiveSessions(False,lock_fd=fd,commissioning=True)
        return WORKER_STATE['readiness'].status()
    if kind == 'session':
        from ffn_fe100_live_sessions import LiveSessions
        if 'live' not in WORKER_STATE: WORKER_STATE['live'] = LiveSessions(True, lock_fd=fd,commissioning=True)
        live = WORKER_STATE['live']
        data = bytes.fromhex(request.get('data', IDENTITY.hex()))
        # A first physical packet can create an identity entry with an ASIC
        # allocated flow ID. Allow exact-key deletion of that learned entry;
        # only the controller's preflight/ownership journal may request it.
        keys=(KEY,RETURN_KEY)+((REVERSE_IDENTITY[:16],) if PAIRED_NAT_LAB else ())
        allowed=(IDENTITY,RETURN_IDENTITY,FORWARD,DROP)+((REVERSE_IDENTITY,REVERSE_FORWARD,REVERSE_DROP) if PAIRED_NAT_LAB else ())
        learned = (len(data)==64 and data[:16] in keys and
                   data==entry4(data[:16],int.from_bytes(data[36:40],'big')))
        if data not in allowed and not (learned and request['op']=='delete'):
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
             'smac':(0x50000,8,'fetch_smac_entry','insert_smac_entry'),
             'txport':(0x10000,3,'get_tx_portmap_entry','set_tx_portmap_entry'),
             'rxport':(0x10000,1,'get_rx_portmap_entry','set_rx_portmap_entry'),
             'nexthop':(0x50000,16,'fetch_nexthop_entry','insert_nexthop_entry')}
    base, size, getname, putname = specs[kind]
    index = request['index']
    if index not in (range(55) if kind == 'parser' else PORT_PAIR if kind in ('txport','rxport') else (52,53,54,55) if kind=='spm' else (*PORT_PROFILE['lif'].values(),31) if kind == 'lif' else (30,31)):
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
    if kind=='rxport':
        # Reference owner ABI: key is swdev/device/BCM port; fetch's third
        # argument is int*, set's is int, delete takes only the key pointer.
        key=(C.c_ubyte*3)(0,0,PORT_PROFILE['physical'][index])
        result=C.c_int(data[0])
        if op not in ('fetch','insert','delete'):raise ValueError('invalid RX map operation')
        fn=getattr(lib,'pan_fe100_'+({'fetch':getname,'insert':putname,'delete':'delete_rx_portmap_entry'}[op]))
        fn.argtypes=[C.c_uint32,C.c_void_p]+([] if op=='delete' else [C.POINTER(C.c_int) if op=='fetch' else C.c_int])
        args=(0,key) if op=='delete' else (0,key,C.byref(result) if op=='fetch' else result.value)
    elif op == 'delete':
        if kind not in ('acl','qm','nexthop','lef','txport','lif','smac'): raise ValueError('delete not supported')
        fn = getattr(lib,'pan_fe100_delete_'+('tx_portmap' if kind=='txport' else kind)+'_entry')
        fn.argtypes = [C.c_uint32]+[C.c_int]*(1 if kind in ('lef','txport','lif','smac') else 2)
        args = (0,index) if kind in ('lef','txport','lif','smac') else (0,1 if kind in ('acl','qm') else 0,index)
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
    if kind=='rxport':entry=bytes((result.value,))
    return {'rc':rc,'data':bytes(entry).hex(),'trace':trace}


def recovery_profile():
    return {'ingress':FRONT_RETURN,'egress':EGRESS,'vlan_return':VLAN_RETURN,'nat_mode':NAT_MODE,
            'internal_mac_loopback':MAC_LOOPBACK,'return_port':RETURN_PORT,'routed_lab':ROUTED_LAB,
            'session_key':KEY.hex(),'return_key':RETURN_KEY.hex(),'pair':PORT_PAIR,
            'paired_nat':PAIRED_NAT_LAB,'ipv6_miss':IPV6_MISS_LAB,'smac_rewrite':SMAC_REWRITE,
            'forward_entry':FORWARD.hex(),'reverse_entry':REVERSE_FORWARD.hex() if REVERSE_FORWARD else None}


def generation_sources(root,boot):
    prefixes=('fhm-','fdt-train-','fdt-recover-','flu-init-','flu-verified-','fcm-','sem-init-','warm-lab-')
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*'+boot+'.json'))
            if p.name.startswith(prefixes)}


class Lab:
    def __init__(self,recovery=None):
        self.lock = open('/run/ffn-fe100-tables.lock','a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:self.lock.close();raise
        self.workers = {}
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if recovery is not None:
            try:
                path=Path(recovery)
                if path.is_symlink() or path.resolve().parent!=ROOT.resolve():raise ValueError('recovery journal outside lab root')
                record=json.loads(path.read_text())
                self.validate_recovery(record,boot,generation_sources(ROOT,boot))
                self.path,self.record=path,record
                self.prepared=bool(record.get('session_touched'))
                return
            except BaseException:self.lock.close();raise
        self.path = ROOT/('packet-session-'+str(time.time_ns())+'.json')
        self.record = {'schema':2,'owner_sha256':SHA,
                       'profile':recovery_profile(),'generation_sources':generation_sources(ROOT,boot),
                       'cp_boot_id':boot,
                       'changes':[], 'snapshots':{}, 'stage':'preflight', 'session_offload_verified':False}
        self.prepared = False
        try:self.save()
        except BaseException:self.lock.close();raise

    @staticmethod
    def validate_recovery(record,boot,sources):
        if (record.get('schema')!=2 or record.get('owner_sha256')!=SHA or record.get('cp_boot_id')!=boot or
                record.get('profile')!=recovery_profile() or not sources or record.get('generation_sources')!=sources):
            raise RuntimeError('lab recovery profile, boot or initialization generation changed')
        if not isinstance(record.get('changes'),list):raise ValueError('invalid lab recovery journal')
        for change in record['changes']:
            if not isinstance(change,dict) or set(change)!={'kind','index','before','wanted','restored'}:
                raise ValueError('invalid lab recovery intent')
            if type(change['restored']) is not bool:raise ValueError('invalid recovery acknowledgement')

    def close(self):
        for process,err in self.workers.values():
            try:process.stdin.close()
            except BrokenPipeError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            process.stdout.close();err.close()
        self.workers.clear();self.lock.close()

    def save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(self.record,f); f.flush(); os.fsync(f.fileno())
        os.replace(temp,self.path)
        fd=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

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
        if PAIRED_NAT_LAB:
            if self.call('session',data=REVERSE_IDENTITY)['rc']!=3:raise RuntimeError('reverse lab flow already owned')
            for kind in ('acl','qm'):
                if self.call(kind,index=30)['rc']!=3:raise RuntimeError('reverse lab resource occupied: '+kind)
        if IPV6_MISS_LAB and self.call('session6')['rc']!=3:
            raise RuntimeError('IPv6 lab miss identity already exists')
        for index in (30,31):
            if self.call('nexthop',index=index)['rc'] != 3: raise RuntimeError('lab next-hop occupied')
        if self.call('acl')['rc'] != 3: raise RuntimeError('lab ACL occupied')
        if SMAC_REWRITE and self.call('smac')['rc']!=3:raise RuntimeError('lab source-MAC slot occupied')
        if VLAN_RETURN:
            if EGRESS==FRONT_RETURN and not MAC_LOOPBACK:raise RuntimeError('VLAN return requires distinct ports')
            if self.call('lif',index=31)['rc']!=3:raise RuntimeError('return LIF31 occupied')
            if self.call('session',data=RETURN_IDENTITY)['rc']!=3:raise RuntimeError('return flow already owned')
        lif = self.call('lif',index=LAB_LIF)
        expected = bytearray(36)
        struct.pack_into('>III',expected,4,0x80050000,8,FRONT_RETURN<<16)
        expected[16:26]=(63<<32).to_bytes(10,'big');expected[26:36]=(FRONT_RETURN<<32).to_bytes(10,'big')
        if not ((MAC_LOOPBACK or PORT_PAIR!=[5,13]) and lif['rc']==3) and (lif['rc'] or bytes.fromhex(lif['data'])[4:] != expected[4:]):
            raise RuntimeError('ingress LIF differs from front-port baseline')
        from ffn_fe100_parser_apply import SOURCE, encode
        source = SOURCE.read_bytes()
        if hashlib.sha256(source).hexdigest() != '6dcbd4fa1e12e5798a55bf22dded9f3fdfc5d0ee90d454d8a4ee88238a1bbddf':
            raise RuntimeError('parser source changed')
        table = json.loads(source)['pan_fe100_parse_table']
        # Parsed traffic can learn a session before the explicit install.
        # Record ownership after absent-key preflight, before enabling parsing,
        # so an interrupted prepare/miss phase also removes learned entries.
        self.prepared=True;self.record['session_touched']=True
        if PAIRED_NAT_LAB:self.record['reverse_session_touched']=True
        self.save()
        # ACL allow is an exact 5-tuple+zone match in the native IPv4 table.
        acl = bytearray(90);struct.pack_into('>I',acl,0,1<<15)
        acl[4:20]=KEY;acl[21:37]=b'\0'+b'\xff'*15
        self.write('acl',31,bytes(acl))
        if PAIRED_NAT_LAB:
            acl[4:20]=REVERSE_IDENTITY[:16]
            self.write('acl',30,bytes(acl))
        front_return='FFN_FE100_FRONT_RETURN' in os.environ
        if front_return:
            for port in sorted({FRONT_RETURN,RETURN_PORT}):
                mapping=bytes((port,));rx=self.call('rxport',index=port)
                if rx['rc'] not in (0,3) or (rx['rc']==0 and rx['data']!=mapping.hex()):
                    raise RuntimeError('RX port mapping conflict')
                if rx['rc']==3:self.write('rxport',port,mapping)
            from ffn_fe100_nexthop import encode_front
            if self.call('lef')['rc']!=3:raise RuntimeError('LEF31 occupied')
            if self.call('qm')['rc']!=3:raise RuntimeError('QMAP31 occupied')
            physical=PORT_PROFILE['physical'][EGRESS]
            mapping=bytes((0,0,physical))
            tx=self.call('txport',index=EGRESS)
            if tx['rc']!=3 and tx['data']!=mapping.hex():raise RuntimeError('TX port mapping conflict')
            if tx['rc']==3:self.write('txport',EGRESS,mapping)
            # XF removes the CPU message header and emits a DSA-tagged frame
            # through NIF. The scoped BCM rule selects RAW_DSA front egress.
            from ffn_fe100_bcm_lab import run
            queues=run({'mode':'queue-status','front_ports':PORT_PAIR})['queue_ids']
            self.record['bcm_queue_ids']=queues;self.save()
            self.write('qm',31,front_qmap(FORWARD,FRONT_RETURN,queues[physical]))
            if PAIRED_NAT_LAB:
                self.write('qm',30,front_qmap(REVERSE_FORWARD,FRONT_RETURN,queues[physical]))
            self.write('lef',31,struct.pack('>IIH',0x80000000|(EGRESS<<16),0,0))
            if SMAC_REWRITE:
                from ffn_fe100_nexthop import encode_smac
                self.write('smac',31,encode_smac('02:52:20:ab:cd:ef'))
            wanted=encode_front(31,dmac='02:52:20:ab:cd:ee',vlan=4000 if VLAN_RETURN else None,
                                smac_index=31 if SMAC_REWRITE else None)
        else:wanted=next_hop(destination=8,dmac='02:52:20:ab:cd:ee')
        self.write('nexthop',31,wanted)
        struct.pack_into('>II',expected,4,0x80040000,(4094<<16)|30)
        if VLAN_RETURN:
            # Packed owner DWARF: VID at key bits49:38; pport at37:32.
            expected[16:26]=((4095<<38)|(63<<32)).to_bytes(10,'big')
            capture=bytearray(expected)
            struct.pack_into('>III',capture,4,0x80050000,(4093<<16)|8,RETURN_PORT<<16)
            capture[26:36]=((4000<<38)|(RETURN_PORT<<32)).to_bytes(10,'big')
            self.record['return_session_touched']=True;self.save()
            self.write('lif',31,bytes(capture))
        self.write('lif',LAB_LIF,bytes(expected))
        for index in range(55):
            wanted = encode(table[str(index)])
            before = self.call('parser',index=index)
            zero = '8000000080000000000000000000000000000000000000000000000000000001'
            if before['data'] not in (zero,wanted.hex()): raise RuntimeError('unexpected parser table')
            if before['data'] != wanted.hex(): self.write('parser',index,wanted)
        # A miss can allocate a plain identity before install is requested.
        # Journal ownership of this preflight-empty exact key so EOF after
        # miss-only commissioning also removes its hardware-learned identity.
        self.prepared=True;self.record['session_touched']=True
        if IPV6_MISS_LAB:self.record['session6_touched']=True
        self.record['stage']='prepared';self.save()

    def remove_session(self,return_flow=False,reverse=False):
        if reverse and (return_flow or not PAIRED_NAT_LAB):raise ValueError('Invalid reverse lab cleanup')
        identity=REVERSE_IDENTITY if reverse else RETURN_IDENTITY if return_flow else IDENTITY
        key=identity[:16]
        owned=(REVERSE_FORWARD,REVERSE_DROP) if reverse else (FORWARD,DROP)
        current = self.call('session',data=identity)
        if current['rc'] == 3: return
        if current['data'] not in tuple(v.hex() for v in owned):
            data=bytes.fromhex(current['data'])
            if not self.prepared or data!=entry4(key,int.from_bytes(data[36:40],'big')):
                raise RuntimeError('session ownership conflict: '+current['data'])
            # The key was absent at preflight and only our reserved lab LIF
            # has this zone. Log the exact learned identity before deletion.
            self.record.setdefault('learned_entries',[]).append(current)
            self.record['session_touched']=True;self.save()
        self.call('session','delete',data=current['data'])
        if self.call('session',data=identity)['rc'] != 3: raise RuntimeError('session delete not verified')

    def restore(self):
        errors=[]
        if self.record.get('session6_touched'):
            try:
                current=self.call('session6')
                if current['rc']==0:
                    raw=bytes.fromhex(current['data'])
                    if raw[16:56]!=IPV6_LAB_KEY or raw[56:76]!=bytes(20) or raw[80:88]!=bytes(8):
                        raise RuntimeError('IPv6 lab identity ownership conflict')
                    self.record['learned_ipv6_identity']=current;self.save()
                    self.call('session6','delete',data=raw)
                elif current['rc']!=3:raise RuntimeError('IPv6 identity lookup failed')
                if self.call('session6')['rc']!=3:raise RuntimeError('IPv6 identity deletion not verified')
            except Exception as e:errors.append(str(e))
        # Only remove a flow after our durable insert intent, never preflight conflicts.
        if self.record.get('session_touched'):
            try: self.remove_session()
            except Exception as e: errors.append(str(e))
        if self.record.get('reverse_session_touched'):
            try:self.remove_session(reverse=True)
            except Exception as e:errors.append(str(e))
        if self.record.get('return_session_touched'):
            try:self.remove_session(return_flow=True)
            except Exception as e:errors.append(str(e))
        if errors:
            # Never restore a next hop/LIF while an owned flow could still
            # reference it. Keep the exact durable intent for recovery.
            self.record['cleanup_errors']=errors;self.record['stage']='recovery_required';self.save()
            raise RuntimeError('; '.join(errors))
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
            if PAIRED_NAT_LAB:
                self.remove_session(reverse=True)
                wanted=REVERSE_FORWARD if op=='install' else REVERSE_DROP
                self.call('session','insert',data=wanted)
                self.call('session','update',data=wanted)
                actual=self.call('session',data=REVERSE_IDENTITY)
                if actual['rc'] or actual['data']!=wanted.hex():raise RuntimeError('reverse session readback mismatch')
        elif op=='remove':
            if self.record.get('session_touched'): self.remove_session()
            if self.record.get('reverse_session_touched'):self.remove_session(reverse=True)
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
            lab.close()


if __name__=='__main__':main()
