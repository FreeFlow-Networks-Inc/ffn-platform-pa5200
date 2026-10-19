#!/usr/bin/env python3
"""MP-owned authenticated DP -> CP session observation relay. No flow admission.

Besides the small status report, the relay publishes its synchronized inventory
as compact rows so the management plane's session views (dashboard counters,
session browser, `show session`) describe the dataplane's table rather than the
MP's own sockets. The inventory is observation data: nothing reads it back into
the planes, and it never claims hardware admission.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import hashlib

from session_stream import Lines,Receiver,send,local_identity,acknowledgement,RELAY_REPORT,RELAY_INVENTORY

REPORT=RELAY_REPORT
INVENTORY=RELAY_INVENTORY
KEY='/run/credentials/ffn-fe100-session-feed.service/plane-agent-key'
CONFIG=Path('/var/lib/ffn-ngfw/config/running-config.xml')
# Seconds the DP's emission timeline may run ahead of relay receipt: a snapshot is buffered by the ssh
# channel in one burst and acknowledged per message through the CP, so the lag scales with session count.
STREAM_BACKLOG=90


def verify_configuration(path,digest):
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise RuntimeError('Committed configuration changed; resynchronization required')


def commands():
    common=['/usr/bin/ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5',
            '-o','ServerAliveInterval=3','-o','ServerAliveCountMax=2','-i',KEY]
    cp=common+['-F','/etc/ffn-ngfw/ssh-cp.conf','ffn-cp',
               'python3 /usr/local/sbin/ffn_fe100_session_stream.py']
    dp=common+['-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
        '-o','ProxyCommand=/usr/bin/ssh -F /etc/ffn-ngfw/ssh-cp.conf -i '+KEY+' -W %h:%p ffn-cp',
        'root@127.1.2.2','ip netns exec ffn-data python3 /usr/local/sbin/ffn_session_stream_dp.py']
    return dp,cp


def write_report(path,value):
    temporary=path.with_suffix('.new')
    with temporary.open('w') as out:
        os.chmod(temporary,0o600)
        json.dump(dict(value,writer=local_identity(),monotonic_time=time.monotonic()),out,separators=(',',':'))
    temporary.replace(path)


def publish(state,path=REPORT):
    write_report(path,state)


class InventoryPublisher:
    """Writes the receiver's compact inventory at most every `interval` seconds while it changes,
    and at least every `heartbeat` seconds so readers can tell a quiet table from a dead relay."""
    def __init__(self,receiver,path=INVENTORY,interval=2,heartbeat=5,clock=time.monotonic,write=write_report):
        self.receiver,self.path,self.interval,self.heartbeat,self.clock,self.write=receiver,path,interval,heartbeat,clock,write
        self.last=None;self.changes=None;self.error=None

    def publish(self,extra=None,force=False):
        now=self.clock();receiver=self.receiver
        if not force and self.last is not None:
            if now-self.last<self.interval:return False
            if receiver.changes==self.changes and now-self.last<self.heartbeat:return False
        state=receiver.status()
        rows=receiver.inventory()
        tcp=sum(r.get('proto')==6 for r in rows);udp=sum(r.get('proto')==17 for r in rows)
        blockers={}
        for row in rows:
            for reason in row['blockers']:blockers[reason]=blockers.get(reason,0)+1
        value=dict(schema=1,mode='observation-only',hardware_admission=False,ready=state['ready'],
                   sequence=state['sequence'],producer=state['producer'],limits=state['limits'],
                   policy_revision=(state['policy'] or {}).get('revision'),reason=state['reason'],
                   counters=dict(sessions=len(rows),software_candidates=state['software_candidates'],
                                 l3_candidates=state['l3_candidates'],hardware_admitted=0,
                                 blocked=len(rows)-state['software_candidates'],tcp=tcp,udp=udp,other=len(rows)-tcp-udp,
                                 nat_source=sum(r['nat']['source'] for r in rows),
                                 nat_destination=sum(r['nat']['destination'] for r in rows),
                                 blockers=dict(sorted(blockers.items(),key=lambda item:(-item[1],item[0])))),
                   sessions=rows,**(extra or {}))
        # The inventory is a report for the session views; failing to write it
        # must never stop the relay keeping the CP synchronized.
        self.last=now;self.changes=receiver.changes
        try:self.write(self.path,value)
        except OSError as error:
            self.error=str(error)[:256];return False
        self.error=None
        return True


def relay(nonce,read_dp,write_cp,read_cp,save=publish,inventory=None):
    receiver=Receiver(nonce,backlog=STREAM_BACKLOG)
    publisher=InventoryPublisher(receiver,**(inventory or {}))
    try:
        save(receiver.status());publisher.publish(force=True)
        while True:
            message=read_dp();state=receiver.accept(message)
            started=time.monotonic();write_cp(message);ack=read_cp();receiver.tick()
            expected=acknowledgement(nonce,state)
            if ack!=expected or time.monotonic()-started>=8:raise ValueError('CP session acknowledgement is stale or mismatched')
            save(dict(state,cp_acknowledged=True));publisher.publish(dict(cp_acknowledged=True))
    finally:
        receiver.fence('Plane stream lost; reconnecting requires a complete snapshot')
        save(dict(receiver.status(),cp_acknowledged=False))
        try:publisher.publish(dict(cp_acknowledged=False),force=True)
        except OSError:pass


def main():
    nonce=str(uuid.uuid4());processes=[]
    with open('/run/ffn-fe100-session-relay.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            from fe100_attachment_config import compile_config,checksum
            raw=CONFIG.read_bytes();configuration=compile_config(raw)
            for command in commands():
                processes.append(subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,bufsize=0))
            dp,cp=processes
            dp_lines=Lines(dp.stdout.fileno());cp_lines=Lines(cp.stdout.fileno())
            send(cp.stdin.fileno(),dict(nonce=nonce,configuration=configuration))
            if cp_lines.read(8)!=dict(config_digest=configuration['config_digest'],intent_digest=checksum(configuration)):
                raise ValueError('CP did not acknowledge committed interface intent')
            send(dp.stdin.fileno(),{'nonce':nonce})
            def current():
                verify_configuration(CONFIG,configuration['config_digest'])
            def read():
                current();message=dp_lines.read(8);current();return message
            def save(state):publish(dict(state,configuration_digest=configuration['config_digest']))
            relay(nonce,read,lambda value:send(cp.stdin.fileno(),value,8),lambda:cp_lines.read(8),save)
        finally:
            for process in processes:
                process.stdin.close();process.terminate()
            for process in processes:
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':main()
