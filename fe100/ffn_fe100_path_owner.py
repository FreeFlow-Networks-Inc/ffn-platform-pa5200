"""Durable FE100 next-hop/source-MAC ownership under the shared table lock.

The caller supplies commissioned index pools and a trusted current-path resolver.
No port, parser, LIF, zone, route or NAT allocation is inferred by this module.
Flow owners must acknowledge draining references BEFORE resources are removed.
"""
import copy
import ipaddress
import json
import re
from ffn_fe100_nexthop import encode_front,encode_smac
from ffn_fe100_policy import digest


class PathOwner:
    def __init__(self,db,backend,pools,boot,current,drain):
        if set(pools)!={'smac','nexthop'}:raise ValueError('Commissioned resource pools required')
        self.pools={}
        for kind,limit in [('smac',1024),('nexthop',65536)]:
            values=pools[kind]
            if (not isinstance(values,(list,tuple)) or not 2<=len(values)<=4096 or
                any(type(i) is not int or not 0<=i<limit for i in values) or len(set(values))!=len(values)):
                raise ValueError('Invalid commissioned '+kind+' pool')
            self.pools[kind]=tuple(values)
        self.db,self.backend,self.boot,self.current,self.drain=db,backend,boot,current,drain
        db.execute('CREATE TABLE IF NOT EXISTS hardware_paths (id TEXT PRIMARY KEY, body TEXT NOT NULL)');db.commit()
        self.paths={key:json.loads(body) for key,body in db.execute('SELECT id,body FROM hardware_paths')}
        self.recovery_required=bool(self.paths)
        for record in self.paths.values():self.validate_record(record)

    @staticmethod
    def key(value):
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('SHA256 path identity required')

    def validate_record(self,r):
        if (set(r)!={'boot','state','plan','resources','snapshot'} or
            r['state'] not in ('installing','ready','removing') or len(r['resources'])!=4):
            raise ValueError('Invalid resource journal; recovery required')
        seen=set()
        for item in r['resources']:
            kind,index=item['kind'],item['index'];raw=bytes.fromhex(item['data'])
            if (kind not in self.pools or index not in self.pools[kind] or (kind,index) in seen or
                    len(raw)!={'smac':8,'nexthop':16}[kind]):raise ValueError('Journal outside commissioned pools')
            seen.add((kind,index))

    def save(self,key,record):
        # Storage failure must also fence a previously usable owner.
        try:
            with self.db:self.db.execute('INSERT OR REPLACE INTO hardware_paths VALUES (?,?)',(key,json.dumps(record)))
        except BaseException:self.recovery_required=True;raise
        self.paths[key]=copy.deepcopy(record)

    @staticmethod
    def payload(kind,raw):
        return raw[1:] if kind=='nexthop' else raw  # hardware generated next-hop ECC

    def matches(self,item,actual):
        return actual is not None and self.payload(item['kind'],actual)==self.payload(item['kind'],bytes.fromhex(item['data']))

    @staticmethod
    def validate_plan(plan):
        if not isinstance(plan,dict) or set(plan)!={'nat_digest','directions'}:raise ValueError('Invalid path plan')
        PathOwner.key(plan['nat_digest'])
        if not isinstance(plan['directions'],list) or len(plan['directions'])!=2:raise ValueError('Two paths required')
        fields={'ingress','egress','destination','zone','egress_lif','source_mac','destination_mac',
                'vlan','mtu','route_revision','neighbor_revision','attachment_revision'}
        for d in plan['directions']:
            if set(d)!=fields:raise ValueError('Incomplete commissioned path')
            for f in ('ingress','egress','zone','egress_lif'):
                if type(d[f]) is not int or not 0<=d[f]<65536:raise ValueError('Invalid '+f)
            if d['ingress']==d['egress']:raise ValueError('Distinct ingress/egress required')
            addr=ipaddress.IPv4Address(d['destination'])
            if str(addr)!=d['destination'] or addr.is_multicast or addr.is_unspecified:raise ValueError('Invalid path destination')
            for f in ('route_revision','neighbor_revision','attachment_revision'):PathOwner.key(d[f])
            encode_smac(d['source_mac'])
            if not d['destination_mac']:raise ValueError('Resolved destination MAC required')
            encode_front(d['egress_lif'],d['destination_mac'],d['mtu'],d['vlan'],0)
        a,b=plan['directions']
        if (a['ingress'],a['egress'])!=(b['egress'],b['ingress']):raise ValueError('Directions do not reverse attachments')

    def allocate(self,kind,count):
        used={r['index'] for p in self.paths.values() for r in p['resources'] if r['kind']==kind}
        result=[]
        for index in self.pools[kind]:
            if index not in used and self.backend.fetch(kind,index) is None:
                result.append(index)
                if len(result)==count:return result
        raise RuntimeError('No empty commissioned '+kind+' slots')

    def acquire(self,key,plan):
        self.key(key);self.validate_plan(plan)
        if self.recovery_required:raise RuntimeError('Path recovery required')
        if key in self.paths:raise ValueError('Path is already owned')
        plan=copy.deepcopy(plan)
        if self.current(key)!=plan:raise RuntimeError('Path generation changed before allocation')
        smacs=self.allocate('smac',2);hops=self.allocate('nexthop',2)
        resources=[];directions=[]
        # Install both source MACs before either referring next hop.
        for i,d in enumerate(plan['directions']):
            resources.append(dict(kind='smac',index=smacs[i],data=encode_smac(d['source_mac']).hex()))
            directions.append({k:d[k] for k in ('ingress','egress','destination','zone','route_revision','neighbor_revision','attachment_revision')} | {'next_hop':hops[i]})
        for i,d in enumerate(plan['directions']):
            raw=encode_front(d['egress_lif'],d['destination_mac'],d['mtu'],d['vlan'],smacs[i])
            resources.append(dict(kind='nexthop',index=hops[i],data=raw.hex()))
        record=dict(boot=self.boot,state='installing',plan=plan,resources=resources,
                    snapshot=dict(nat_digest=plan['nat_digest'],directions=directions))
        self.save(key,record)
        try:
            for item in resources:
                if self.current(key)!=plan:raise RuntimeError('Path changed during allocation')
                if self.backend.fetch(item['kind'],item['index']) is not None:raise RuntimeError('Resource ownership changed')
                self.backend.insert(item['kind'],item['index'],bytes.fromhex(item['data']))
                if not self.matches(item,self.backend.fetch(item['kind'],item['index'])):raise RuntimeError('Resource readback mismatch')
            if self.current(key)!=plan:raise RuntimeError('Path changed during allocation')
            self.save(key,record | {'state':'ready'})
            return self.snapshot(key)
        except BaseException:
            self.recovery_required=True
            # Leave durable intent, including ambiguous writes, for exact recovery.
            raise

    def snapshot(self,key):
        self.key(key)
        if self.recovery_required:raise RuntimeError('Path recovery required')
        record=self.paths[key]
        if record['state']!='ready' or record['boot']!=self.boot or self.current(key)!=record['plan']:
            raise RuntimeError('Path generation or owner changed')
        for item in record['resources']:
            if not self.matches(item,self.backend.fetch(item['kind'],item['index'])):raise RuntimeError('Hardware path readback changed')
        return copy.deepcopy(record['snapshot'])

    def release(self,key):
        self.key(key)
        if key not in self.paths:return
        try:
            record=self.paths[key]
            # No resource deletion until all dependent flow deletes read back.
            if self.drain(key) is not True:raise RuntimeError('Dependent session drain is not acknowledged')
            self.save(key,record | {'state':'removing'})
            for item in reversed(record['resources']):
                actual=self.backend.fetch(item['kind'],item['index'])
                if actual is not None:
                    if record['boot']!=self.boot or not self.matches(item,actual):
                        raise RuntimeError('Resource ownership conflict; manual recovery required')
                    self.backend.delete(item['kind'],item['index'])
                if self.backend.fetch(item['kind'],item['index']) is not None:raise RuntimeError('Resource removal not acknowledged')
            with self.db:self.db.execute('DELETE FROM hardware_paths WHERE id=?',(key,))
            del self.paths[key]
        except BaseException:self.recovery_required=True;raise

    def recover(self):
        self.recovery_required=True
        for key in list(self.paths):self.release(key)
        self.recovery_required=False

    def reconcile(self):
        if self.recovery_required:self.recover();return
        for key in list(self.paths):
            try:self.snapshot(key)
            except Exception:self.release(key)
