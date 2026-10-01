"""Ordered DP observations owned by the supervised CP control service.

No observation is an admission verdict. Bounded fragments carry one existing
session-stream frame through the small local RPC socket. Only a complete,
digest-checked frame can advance the acknowledged sequence. Loss of continuity
withdraws existing policy ownership before another snapshot is accepted.
"""
import base64
import hashlib
import json
import time
import uuid
from session_stream import Receiver,MAX_FRAME,acknowledgement,canonical_uuid,sha

CHUNK=32768
INVENTORY_BYTES=32*1024*1024


class Observations:
    def __init__(self,withdraw,clock=time.monotonic,configuration_digest=None):
        self.withdraw,self.clock=withdraw,clock
        self.receiver=None;self.pending=None;self.sizes={};self.reason='awaiting MP relay'
        self.configuration=None;self.configuration_digest=configuration_digest

    def fence(self,reason):
        self.pending=None;self.sizes.clear();self.reason=str(reason)[:256]
        if self.receiver is not None:self.receiver.fence(reason)
        self.withdraw()

    def start(self,nonce):
        canonical_uuid(nonce)
        if self.receiver is not None and nonce==self.receiver.nonce:
            raise ValueError('observation relay nonce cannot be replayed')
        self.fence('new relay requires complete snapshot')
        self.configuration=None
        self.receiver=Receiver(nonce,clock=self.clock)
        return self.status()

    def tick(self):
        if self.receiver is None:return
        try:
            self.receiver.tick()
            if self.pending is not None and self.clock()-self.pending['started']>=5:
                raise TimeoutError('observation frame assembly expired')
        except TimeoutError as error:
            self.fence(error)
            self.receiver=None

    def close(self,nonce):
        canonical_uuid(nonce)
        # A delayed disconnect from an old relay cannot withdraw its successor.
        if self.receiver is not None and nonce==self.receiver.nonce:
            self.fence('MP relay disconnected');self.receiver=None
        return self.status()

    def chunk(self,payload):
        self.tick()
        if self.receiver is None or payload.get('nonce')!=self.receiver.nonce:
            raise ValueError('observation relay is not synchronized; start a new snapshot')
        try:
            if set(payload)!={'nonce','transfer','offset','total','digest','data'}:
                raise ValueError('invalid observation fragment fields')
            canonical_uuid(payload['transfer']);sha(payload['digest'])
            offset,total=payload['offset'],payload['total']
            if (type(offset) is not int or type(total) is not int or not 0<=offset<total<=MAX_FRAME or
                    not isinstance(payload['data'],str) or len(payload['data'])>4*((CHUNK+2)//3)):
                raise ValueError('invalid observation fragment bounds')
            data=base64.b64decode(payload['data'],validate=True)
            if not 1<=len(data)<=CHUNK or offset+len(data)>total:
                raise ValueError('invalid observation fragment length')
            if self.pending is None:
                if offset!=0:raise ValueError('observation fragment sequence gap')
                self.pending=dict(transfer=payload['transfer'],total=total,digest=payload['digest'],
                                  started=self.clock(),data=bytearray())
            p=self.pending
            if (any(p[k]!=payload[k] for k in ('transfer','total','digest')) or offset!=len(p['data'])):
                raise ValueError('observation fragment replay or replacement')
            p['data'].extend(data)
            result=dict(transfer=p['transfer'],received=len(p['data']),complete=False)
            if len(p['data'])!=total:return result
            raw=bytes(p['data']);self.pending=None
            if hashlib.sha256(raw).hexdigest()!=p['digest']:raise ValueError('observation frame digest mismatch')
            message=json.loads(raw)
            if not isinstance(message,dict):raise ValueError('observation frame must be an object')
            if 'configuration' in message:
                from fe100_attachment_config import validate,checksum
                if (set(message)!={'schema','nonce','configuration'} or type(message['schema']) is not int or
                        message['schema']!=1 or message['nonce']!=self.receiver.nonce or
                        self.receiver.producer is not None or self.configuration is not None):
                    raise ValueError('Interface intent requires a new relay before the DP snapshot')
                config=validate(message['configuration'])
                if self.configuration_digest is None or config['config_digest']!=self.configuration_digest():
                    raise ValueError('Interface intent does not match the committed policy barrier')
                self.configuration=config
                return dict(result,complete=True,ack=dict(config_digest=config['config_digest'],intent_digest=checksum(config)))
            # Invalidation is ordered ahead of accepting a new applied context.
            if message.get('operation')=='begin' or 'unavailable' in message:
                self.fence('DP policy, topology or producer changed')
            state=self.receiver.accept(message)
            op=message.get('operation');body=message.get('payload')
            if op in ('snapshot','upsert'):
                for row in (body if op=='snapshot' else [body]):
                    self.sizes[row['identity']]=len(json.dumps(row,separators=(',',':'),allow_nan=False).encode())
            elif op=='close':self.sizes.pop(body['identity'],None)
            if sum(self.sizes.values())>INVENTORY_BYTES:raise ValueError('observation inventory memory limit exceeded')
            self.reason=self.receiver.reason
            return dict(result,complete=True,ack=acknowledgement(self.receiver.nonce,state))
        except Exception as error:
            self.fence(error);self.receiver=None
            raise

    def status(self):
        state=self.receiver.status() if self.receiver is not None else {}
        return dict(mode='supervised-observation-only',hardware_admission=False,
                    ready=state.get('ready',False),sequence=state.get('sequence',0),
                    sessions=state.get('sessions',0),software_candidates=state.get('software_candidates',0),
                    l3_candidates=state.get('l3_candidates',0),producer=state.get('producer'),
                    policy_digest=(state.get('policy') or {}).get('digest'),
                    nat_digest=(state.get('policy') or {}).get('nat_digest'),
                    topology_digest=((state.get('policy') or {}).get('l3') or {}).get('snapshot_digest'),
                    configuration_digest=self.configuration['config_digest'] if self.receiver is not None and self.configuration is not None else None,
                    pending_bytes=len(self.pending['data']) if self.pending else 0,reason=self.reason)


class ObservationClient:
    def __init__(self,nonce,rpc=None):
        if rpc is None:
            from ffn_fe100_control_socket import request
            rpc=request
        self.rpc,self.nonce=rpc,canonical_uuid(nonce)
        result=rpc('observe-start',dict(nonce=nonce))
        self.owner=canonical_uuid(result['control_owner'])

    def send(self,message):
        raw=json.dumps(message,separators=(',',':'),allow_nan=False).encode()
        if not 1<=len(raw)<=MAX_FRAME:raise ValueError('observation frame exceeds limit')
        transfer=str(uuid.uuid4());checksum=hashlib.sha256(raw).hexdigest()
        for offset in range(0,len(raw),CHUNK):
            data=raw[offset:offset+CHUNK]
            result=self.rpc('observe-chunk',dict(control_owner=self.owner,nonce=self.nonce,
                transfer=transfer,offset=offset,total=len(raw),digest=checksum,
                data=base64.b64encode(data).decode('ascii')))
            expected_complete=offset+len(data)==len(raw)
            if (result.get('control_owner')!=self.owner or result.get('transfer')!=transfer or
                    type(result.get('received')) is not int or result['received']!=offset+len(data) or
                    result.get('complete') is not expected_complete):
                raise RuntimeError('observation owner or fragment acknowledgement changed')
        return result['ack']

    def close(self):
        return self.rpc('observe-close',dict(control_owner=self.owner,nonce=self.nonce))
