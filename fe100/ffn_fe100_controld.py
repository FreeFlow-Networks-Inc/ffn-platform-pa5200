#!/usr/bin/env python3
"""Supervised CP policy barrier service; hardware admission stays gated.

The worker owns the policy journal for its whole lifetime. The independent
parent fences dead/stalled workers and recovers exact durable session intent.
This version exposes policy barriers and observation intake, never installation.
"""
import json
import os
from pathlib import Path
import select
import socket
import stat
import subprocess
import sys
import time
import uuid
from ffn_fe100_guard import Lease,publish,supervise
from ffn_fe100_control_socket import SOCKET,receive,send,root_peer
from ffn_fe100_policy_control import ROOT,PolicyController,control


def drained(state):
    return (isinstance(state,dict) and state.get('phase')=='blocked' and
            type(state.get('sessions')) is int and state['sessions']==0 and
            state.get('recovery_required') is False and state.get('admission_enabled') is False)


def dispatch(owner,message):
    if (set(message)!={'schema','id','operation','payload'} or
            type(message['schema']) is not int or message['schema']!=1 or
            not isinstance(message['id'],str) or str(uuid.UUID(message['id']))!=message['id']):
        raise ValueError('invalid control request envelope')
    return owner.execute(message['operation'],message['payload'])


def serve(path=SOCKET):
    lease=Lease();owner=PolicyController();listener=None;socket_identity=None
    try:
        if not drained(owner.execute('reconcile',{})):raise RuntimeError('initial session drain incomplete')
        lease.pulse()
        if path.exists():
            info=path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0:
                raise RuntimeError('refusing to replace non-owned control socket')
            path.unlink()
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        listener.bind(str(path));info=path.lstat();socket_identity=(info.st_dev,info.st_ino)
        os.chmod(path,0o600);listener.listen(16)
        tick=time.monotonic()+2
        while True:
            if time.monotonic()>=tick:
                if not drained(owner.execute('reconcile',{})):raise RuntimeError('periodic drain incomplete')
                tick=time.monotonic()+2
            lease.pulse()
            if not select.select([listener],[],[],min(1,max(0,tick-time.monotonic())))[0]:continue
            connection,_=listener.accept()
            with connection:
                connection.settimeout(.5)
                identity=None
                try:
                    root_peer(connection);message=receive(connection);identity=message.get('id')
                except (ValueError,PermissionError) as error:
                    try:send(connection,dict(schema=1,id=identity,ok=False,error=str(error)[:512]))
                    except OSError:pass
                    continue
                except (BrokenPipeError,ConnectionResetError,socket.timeout):continue
                try:
                    result=dispatch(owner,message)
                    result=dict(result,control_owner=os.environ['FFN_FE100_GUARD_NONCE'])
                    response=dict(schema=1,id=identity,ok=True,result=result)
                except ValueError as error:
                    # Invalid revisions/requests are harmless only after the
                    # same owner verifies no partial operation remains.
                    if not drained(owner.execute('reconcile',{})):raise RuntimeError('request recovery incomplete')
                    response=dict(schema=1,id=identity,ok=False,error=str(error)[:512])
                try:send(connection,response)
                except (BrokenPipeError,ConnectionResetError,socket.timeout):
                    # A client can disappear after a completed mutation. Its
                    # revision-bound retry cannot silently replay that write.
                    pass
    finally:
        if listener is not None:listener.close()
        if socket_identity is not None:
            try:
                info=path.lstat()
                if (info.st_dev,info.st_ino)==socket_identity:path.unlink()
            except FileNotFoundError:pass
        owner.close()


def recover(reason):
    process=subprocess.run([sys.executable,__file__,'--recover'],capture_output=True,text=True,timeout=30)
    if process.returncode:raise RuntimeError('CP recovery failed: '+process.stderr[-512:])
    if not drained(json.loads(process.stdout)):raise RuntimeError('CP withdrawal not acknowledged')
    return True


def main():
    if sys.argv[1:]==['--worker']:serve();return
    if sys.argv[1:]==['--recover']:
        print(json.dumps(control('reconcile',{})));return
    if sys.argv[1:]:raise ValueError('no public admission command')
    ROOT.mkdir(parents=True,exist_ok=True)
    # Once managed, client failure must never bypass the service by starting
    # another owner. Keep this durable requirement across service restarts.
    publish(ROOT/'control-service-required',dict(schema=1,service='ffn-fe100-control'))
    try:supervise([sys.executable,'-u',__file__,'--worker'],recover,ROOT/'control-guard.json',timeout=25,startup_timeout=45)
    except InterruptedError as error:
        # systemd's SIGTERM. The guardian stops the owner and runs the final
        # drain before re-raising this, so a requested stop is a clean exit,
        # not a failure to restart from.
        print(str(error),flush=True);return
    raise RuntimeError('CP owner stopped after withdrawal; service restart required')


if __name__=='__main__':main()
