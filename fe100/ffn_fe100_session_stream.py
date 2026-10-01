#!/usr/bin/env python3
"""CP shadow receiver for authenticated MP relay input. No hardware API access."""
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
    value=dict(receiver.status(),writer=local_identity(),monotonic_time=time.monotonic())
    temporary=path.with_suffix('.new')
    with temporary.open('w') as out:
        os.chmod(temporary,0o600);json.dump(value,out);out.flush()
    temporary.replace(path)


def serve(nonce,read,write,save=publish):
    receiver=Receiver(nonce)
    try:
        save(receiver)
        while True:
            message=read();state=receiver.accept(message);save(receiver)
            write(acknowledgement(nonce,state))
    finally:
        receiver.fence('MP relay disconnected; a new snapshot is required');save(receiver)


if __name__=='__main__':
    try:
        with open('/run/ffn-fe100-session-stream.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            lines=Lines(0);request=lines.read()
            if set(request)!={'nonce'}:raise ValueError('Stream start accepts only a nonce')
            serve(request['nonce'],lambda:lines.read(10),lambda value:send(1,value))
    except (Exception,KeyboardInterrupt) as error:
        print(str(error)[:512],file=sys.stderr);raise SystemExit(2)
