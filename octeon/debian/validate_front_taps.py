#!/usr/bin/env python3
"""Bounded TAP -> PKO -> front DAC -> PKI -> TAP integration test.

Requires two unconfigured, down front TAPs and their existing physical return
routes. Restores link state and terminates the test transport on every exit.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time
import uuid


def ip(*args):
    return subprocess.check_output(['ip','-n','ffn-data',*args],text=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ports',required=True)
    a=p.parse_args()
    ports=[int(x) for x in a.ports.split(',')]
    if len(ports)!=2 or len(set(ports))!=2 or any(not 1<=x<=24 for x in ports):
        p.error('exactly two front ports required')
    names=['p'+str(x) for x in ports]
    links={n:json.loads(ip('-j','addr','show',n))[0] for n in names}
    if any('UP' in r['flags'] or r.get('master') or r.get('addr_info') for r in links.values()):
        raise RuntimeError('test requires down, unaddressed, unbridged TAPs')
    original=os.open('/proc/self/ns/net',os.O_RDONLY)
    target=os.open('/run/netns/ffn-data',os.O_RDONLY)
    sockets={}; process=None; changed=[]; matched=[]
    lock=open('/run/ffn-network.lock','a')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for name in names:
            ip('link','set',name,'up'); changed.append(name)
        fcntl.flock(lock,fcntl.LOCK_UN)
        process=subprocess.Popen([sys.executable,'/usr/local/sbin/ffn_dp_packet_transport.py',
            '--rx','ffnpkt0','--tx','ffnpkt0','--ports',a.ports,'--rx-format','bcm-otmh-ssp',
            '--seconds','15'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        deadline=time.monotonic()+8
        while not all('LOWER_UP' in json.loads(ip('-j','link','show',n))[0]['flags'] for n in names):
            if process.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError('TAP transport did not start')
            time.sleep(.1)
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Bind only after the device is operational. Binding a down TAP can
        # leave ENETDOWN pending on the packet socket after the link comes up.
        for name in names: ip('link','set',name,'up')
        os.setns(target,0)
        for name in names:
            sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
            sockets[name]=sock; sock.bind((name,0)); sock.setblocking(False)
        os.setns(original,0)
        for size in (64,1514):
            for source,destination in (names,names[::-1]):
                mac=lambda n:bytes.fromhex(links[n]['address'].replace(':',''))
                frame=mac(destination)+mac(source)+b'\x88\xb5'+uuid.uuid4().bytes
                frame+=bytes(i%256 for i in range(size-len(frame)))
                if sockets[source].send(frame)!=size: raise RuntimeError('short TAP transmit')
                deadline=time.monotonic()+2
                found=False
                while time.monotonic()<deadline:
                    if not select.select([sockets[destination]],[],[],.1)[0]: continue
                    data,addr=sockets[destination].recvfrom(65536)
                    if addr[2]!=socket.PACKET_OUTGOING and data==frame:
                        found=True;break
                if not found: raise RuntimeError('no exact TAP return: '+source+' -> '+destination)
                matched.append({'source':source,'destination':destination,'frame_size':size})
    finally:
        os.setns(original,0)
        if process:
            if process.poll() is None: process.terminate()
            try: output=process.communicate(timeout=4)[0]
            except subprocess.TimeoutExpired:
                process.kill();output=process.communicate()[0]
        else: output=''
        for name in reversed(changed): ip('link','set',name,'down')
        lock.close()
        for sock in sockets.values(): sock.close()
        os.close(original);os.close(target)
        print(json.dumps({'schema':1,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'matched_rx':matched,'tap_physical_round_trip_verified':len(matched)==4,
            'transport_output':output,'test_links_restored_down':all('UP' not in json.loads(ip('-j','link','show',n))[0]['flags'] for n in changed),
            'session_offload_verified':False},indent=2))


if __name__=='__main__':main()
