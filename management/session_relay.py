#!/usr/bin/env python3
"""MP-owned authenticated DP -> CP session observation relay. No flow admission."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from session_stream import Lines,Receiver,send,local_identity

REPORT=Path('/run/ffn-fe100-session-relay.json')
KEY='/run/credentials/ffn-fe100-session-feed.service/plane-agent-key'


def commands():
    common=['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
            '-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-i',KEY]
    cp=common+['-F','/etc/ffn-ngfw/ssh-cp.conf','ffn-cp',
               'python3 /usr/local/sbin/ffn_fe100_session_stream.py']
    dp=common+['-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
        '-o','ProxyCommand=/usr/bin/ssh -F /etc/ffn-ngfw/ssh-cp.conf -i '+KEY+' -W %h:%p ffn-cp',
        'root@127.1.2.2','ip netns exec ffn-data python3 /usr/local/sbin/ffn_session_stream_dp.py']
    return dp,cp


def publish(state,path=REPORT):
    temporary=path.with_suffix('.new')
    with temporary.open('w') as out:
        os.chmod(temporary,0o600)
        json.dump(dict(state,writer=local_identity(),monotonic_time=time.monotonic()),out)
    temporary.replace(path)


def relay(nonce,read_dp,write_cp,read_cp,save=publish):
    receiver=Receiver(nonce)
    try:
        save(receiver.status())
        while True:
            message=read_dp();state=receiver.accept(message)
            started=time.monotonic();write_cp(message);ack=read_cp();receiver.tick()
            expected=dict(schema=1,nonce=nonce,producer=state['producer'],sequence=state['sequence'],
                          ready=state['ready'],sessions=state['sessions'],hardware_admission=False)
            if ack!=expected or time.monotonic()-started>=8:raise ValueError('CP session acknowledgement is stale or mismatched')
            save(dict(state,cp_acknowledged=True))
    finally:
        receiver.fence('Plane stream lost; reconnecting requires a complete snapshot')
        save(dict(receiver.status(),cp_acknowledged=False))


def main():
    nonce=str(uuid.uuid4());processes=[]
    with open('/run/ffn-fe100-session-relay.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            for command in commands():
                processes.append(subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,bufsize=0))
            dp,cp=processes
            for process in (dp,cp):send(process.stdin.fileno(),{'nonce':nonce})
            dp_lines=Lines(dp.stdout.fileno());cp_lines=Lines(cp.stdout.fileno())
            relay(nonce,lambda:dp_lines.read(8),lambda value:send(cp.stdin.fileno(),value,8),lambda:cp_lines.read(8))
        finally:
            for process in processes:
                process.stdin.close();process.terminate()
            for process in processes:
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':main()
