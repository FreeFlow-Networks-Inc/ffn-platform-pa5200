#!/usr/bin/env python3
"""Authenticated MP relay adapter for the supervised CP observation owner."""
import fcntl
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,'/usr/local/lib/ffn')
from session_stream import Lines,Receiver,send,local_identity,acknowledgement

REPORT=Path('/var/lib/ffn/fe100/session-stream.json')


def publish(receiver,path=REPORT):
    path.parent.mkdir(parents=True,exist_ok=True)
    value=dict(receiver.status(),writer=local_identity(),monotonic_time=time.monotonic(),
               control_owner=getattr(receiver,'control_owner',None),
               owner_acknowledged=getattr(receiver,'owner_acknowledged',False))
    temporary=path.with_suffix('.new')
    with temporary.open('w') as out:
        os.chmod(temporary,0o600);json.dump(value,out);out.flush()
    temporary.replace(path)


def serve(nonce,read,write,save=publish,bridge_factory=None,configuration=None):
    if bridge_factory is None:
        from ffn_fe100_observations import ObservationClient
        bridge_factory=ObservationClient
    receiver=Receiver(nonce)
    bridge=bridge_factory(nonce)
    receiver.control_owner=getattr(bridge,'owner',None);receiver.owner_acknowledged=False
    try:
        if configuration is not None:
            from fe100_attachment_config import validate,checksum
            validate(configuration)
            ack=bridge.send(dict(schema=1,nonce=nonce,configuration=configuration))
            if ack!=dict(config_digest=configuration['config_digest'],intent_digest=checksum(configuration)):
                raise ValueError('Supervised interface intent acknowledgement disagrees')
            write(ack)
        save(receiver)
        while True:
            message=read();state=receiver.accept(message)
            ack=bridge.send(message);receiver.tick()
            if ack!=acknowledgement(nonce,state):raise ValueError('supervised CP owner acknowledgement disagrees')
            receiver.owner_acknowledged=True
            save(receiver);write(ack)
    finally:
        try:bridge.close()
        finally:
            receiver.owner_acknowledged=False
            receiver.fence('MP relay disconnected; a new snapshot is required');save(receiver)


if __name__=='__main__':
    try:
        with open('/run/ffn-fe100-session-stream.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            lines=Lines(0);request=lines.read()
            if set(request) not in ({'nonce'},{'nonce','configuration'}):raise ValueError('Invalid stream start')
            serve(request['nonce'],lambda:lines.read(10),lambda value:send(1,value),configuration=request.get('configuration'))
    except (Exception,KeyboardInterrupt) as error:
        print(str(error)[:512],file=sys.stderr);raise SystemExit(2)
