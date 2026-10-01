"""Durable ingress port/VLAN and per-member egress table ownership.

Internal control component. A trusted commissioner supplies current mappings,
reserved pools and withdrawal/drain callbacks. This does not enable BCM ingress,
select an aggregate egress member, create queues or qualify an exception path.
"""
import copy
import ctypes as C
import json
import re
from ffn_fe100_policy import digest


class NativeEncoder:
    def __init__(self):
        self.lib=C.CDLL('/usr/local/lib/libffn-fe100-resources.so')
        self.lib.ffn_fe100_lif_encode.argtypes=[C.c_uint]*4+[C.c_void_p,C.c_size_t]
        self.lib.ffn_fe100_lif_encode.restype=C.c_int
        self.lib.ffn_fe100_lef_encode.argtypes=[C.c_uint,C.c_void_p,C.c_size_t]
        self.lib.ffn_fe100_lef_encode.restype=C.c_int

    def __call__(self,plan,port):
        AttachmentOwner.validate_plan(plan)
        if type(port) is not int or port not in plan['ports']:raise ValueError('Port outside attachment')
        lif=(C.c_ubyte*36)();lef=(C.c_ubyte*10)()
        if (self.lib.ffn_fe100_lif_encode(port,plan['vlan'],plan['zone'],plan['miss_next_hop'],lif,len(lif)) or
                self.lib.ffn_fe100_lef_encode(port,lef,len(lef))):
            raise RuntimeError('Native attachment encoding failed')
        return dict(lif=bytes(lif),lef=bytes(lef))


class AttachmentOwner:
    def __init__(self,db,backend,pools,boot,current,withdraw,drain,encode):
        if set(pools)!={'lif','lef'}:raise ValueError('Explicit LIF/LEF pools required')
        self.pools={}
        for kind,values in pools.items():
            if (not isinstance(values,(list,tuple)) or not 1<=len(values)<=32 or
                    any(type(i) is not int or not 0<=i<32 for i in values) or len(set(values))!=len(values)):
                raise ValueError('Invalid commissioned attachment pool')
            self.pools[kind]=tuple(values)
        if not isinstance(boot,str) or not boot:raise ValueError('Hardware boot identity required')
        self.db,self.backend,self.boot=db,backend,boot
        self.current,self.withdraw,self.drain,self.encode=current,withdraw,drain,encode
        if db.in_transaction or db.execute('PRAGMA synchronous').fetchone()[0]<2:
            raise ValueError('Independent FULL synchronous journal required')
        db.execute('CREATE TABLE IF NOT EXISTS hardware_attachments (id TEXT PRIMARY KEY, body TEXT NOT NULL)');db.commit()
        self.rows={name:json.loads(body) for name,body in db.execute('SELECT id,body FROM hardware_attachments')}
        self.recovery_required=bool(self.rows)
        used=set()
        for name,record in self.rows.items():
            self.validate_record(name,record)
            for item in record['resources']:
                key=item['kind'],item['index']
                if key in used:raise ValueError('Duplicate resource in attachment journal')
                used.add(key)

    @staticmethod
    def name(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}',value):
            raise ValueError('Invalid attachment name')

    @staticmethod
    def validate_plan(p):
        if not isinstance(p,dict) or set(p)!={'interface','binding_revision','zone','vlan','ports','miss_next_hop'}:
            raise ValueError('Complete commissioned attachment plan required')
        AttachmentOwner.name(p['interface'])
        if not isinstance(p['binding_revision'],str) or not re.fullmatch('[0-9a-f]{64}',p['binding_revision']):
            raise ValueError('Verified attachment generation required')
        for field,limit in (('zone',65536),('vlan',4095),('miss_next_hop',65536)):
            if type(p[field]) is not int or not 0<=p[field]<limit:raise ValueError('Invalid '+field)
        ports=p['ports']
        if (not isinstance(ports,list) or not 1<=len(ports)<=8 or
                any(type(v) is not int or not 1<=v<=63 for v in ports) or ports!=sorted(set(ports))):
            raise ValueError('Sorted unique commissioned logical ports required')

    def validate_record(self,name,r):
        self.name(name)
        if (not isinstance(r,dict) or set(r)!={'boot','state','plan','resources'} or
                r['state'] not in ('installing','ready','removing') or not isinstance(r['boot'],str)):
            raise ValueError('Invalid attachment journal')
        self.validate_plan(r['plan'])
        if name!=r['plan']['interface']:raise ValueError('Attachment journal identity mismatch')
        expected=[(kind,port) for kind in ('lef','lif') for port in r['plan']['ports']]
        if not isinstance(r['resources'],list) or len(r['resources'])!=len(expected):raise ValueError('Incomplete attachment resources')
        for item,(kind,port) in zip(r['resources'],expected):
            if (set(item)!={'kind','index','port','data'} or item['kind']!=kind or item['port']!=port or
                    type(item['index']) is not int or item['index'] not in self.pools[kind] or
                    item['data']!=self.encode(r['plan'],port)[kind].hex()):
                raise ValueError('Attachment journal outside commissioned scope')

    def save(self,name,record):
        try:
            if self.db.in_transaction:raise RuntimeError('Attachment journal requires its own durable transaction')
            with self.db:self.db.execute('INSERT OR REPLACE INTO hardware_attachments VALUES (?,?)',(name,json.dumps(record)))
        except BaseException:self.recovery_required=True;raise
        self.rows[name]=copy.deepcopy(record)

    def allocate(self,kind,count):
        used={r['index'] for a in self.rows.values() for r in a['resources'] if r['kind']==kind}
        result=[]
        for index in self.pools[kind]:
            if index not in used and self.backend.fetch(kind,index) is None:
                result.append(index)
                if len(result)==count:return result
        raise RuntimeError('No empty commissioned '+kind+' slots')

    def acquire(self,name,plan):
        self.name(name);self.validate_plan(plan)
        if name!=plan['interface']:raise ValueError('Attachment identity mismatch')
        if self.recovery_required:raise RuntimeError('Attachment recovery required')
        if name in self.rows:raise ValueError('Attachment already owned')
        plan=copy.deepcopy(plan)
        if self.current(name)!=plan:raise RuntimeError('Attachment generation changed')
        # The same wire selector cannot belong to two zones/interfaces, including
        # an interrupted installation. Tagged and untagged selectors are distinct.
        for record in self.rows.values():
            other=record['plan']
            if other['vlan']==plan['vlan'] and set(other['ports'])&set(plan['ports']):
                raise ValueError('Ingress port/VLAN already owned')
        slots={k:self.allocate(k,len(plan['ports'])) for k in ('lef','lif')}
        resources=[dict(kind=k,index=slots[k][i],port=p,data=self.encode(plan,p)[k].hex())
                   for k in ('lef','lif') for i,p in enumerate(plan['ports'])]
        record=dict(boot=self.boot,state='installing',plan=plan,resources=resources)
        self.validate_record(name,record);self.save(name,record)
        try:
            for item in resources:
                if self.current(name)!=plan:raise RuntimeError('Attachment changed during installation')
                if self.backend.fetch(item['kind'],item['index']) is not None:raise RuntimeError('Attachment resource occupied')
                raw=bytes.fromhex(item['data'])
                self.backend.insert(item['kind'],item['index'],raw)
                if self.backend.fetch(item['kind'],item['index'])!=raw:raise RuntimeError('Attachment readback mismatch')
            if self.current(name)!=plan:raise RuntimeError('Attachment changed during installation')
            self.save(name,record|{'state':'ready'})
            return self.snapshot(name)
        except BaseException:self.recovery_required=True;raise

    def snapshot(self,name):
        if self.recovery_required:raise RuntimeError('Attachment recovery required')
        r=self.rows[name]
        if r['state']!='ready' or r['boot']!=self.boot or self.current(name)!=r['plan']:
            raise RuntimeError('Attachment generation changed')
        for item in r['resources']:
            if self.backend.fetch(item['kind'],item['index'])!=bytes.fromhex(item['data']):
                raise RuntimeError('Attachment readback changed')
        if self.current(name)!=r['plan']:raise RuntimeError('Attachment changed during readback')
        return dict(plan=copy.deepcopy(r['plan']),revision=digest(r),
                    ingress_lifs={str(i['port']):i['index'] for i in r['resources'] if i['kind']=='lif'},
                    egress_lifs={str(i['port']):i['index'] for i in r['resources'] if i['kind']=='lef'},
                    hardware_admission=False)

    def release(self,name):
        self.name(name)
        if name not in self.rows:return
        try:
            r=self.rows[name]
            if self.withdraw(name) is not True:raise RuntimeError('Ingress withdrawal not acknowledged')
            if self.drain(name) is not True:raise RuntimeError('Dependent flow/path drain not acknowledged')
            self.save(name,r|{'state':'removing'})
            # LIF removal precedes LEF reclamation; all dependent sessions and
            # next hops must already be gone. Never delete foreign entries.
            for item in reversed(r['resources']):
                actual=self.backend.fetch(item['kind'],item['index'])
                if actual is not None:
                    if r['boot']!=self.boot or actual!=bytes.fromhex(item['data']):
                        raise RuntimeError('Attachment ownership conflict; recovery required')
                    self.backend.delete(item['kind'],item['index'])
                if self.backend.fetch(item['kind'],item['index']) is not None:
                    raise RuntimeError('Attachment removal not acknowledged')
            if self.db.in_transaction:raise RuntimeError('Attachment removal requires durable transaction')
            with self.db:self.db.execute('DELETE FROM hardware_attachments WHERE id=?',(name,))
            del self.rows[name]
        except BaseException:self.recovery_required=True;raise

    def recover(self):
        self.recovery_required=True
        for name in list(self.rows):self.release(name)
        self.recovery_required=False

    def reconcile(self):
        if self.recovery_required:self.recover();return
        for name in list(self.rows):
            try:self.snapshot(name)
            except Exception:self.release(name)
