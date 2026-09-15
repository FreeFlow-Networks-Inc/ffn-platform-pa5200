#!/usr/bin/env python3
"""Bounded tagged DAC test between explicitly named VIFs, run on DP."""
import json
import errno
import os
import select
import socket
import subprocess
import sys
import time
import uuid
from ffn_dp_packet_transport import decode_otmh_ssp

sender,receiver=sys.argv[1:3]
if {sender,receiver}!={'fv4001','fv4002'}:raise ValueError('lab VIF pair required')
wire=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));wire.bind(('ffnpkt0',0));wire.setblocking(False)
fd=os.open('/run/netns/ffn-data',os.O_RDONLY);os.setns(fd,0);os.close(fd)
links={p['ifname']:p for p in json.loads(subprocess.check_output(['ip','-j','link']))}
mac=lambda name:bytes.fromhex(links[name]['address'].replace(':',''))
nonce=uuid.uuid4().bytes
frames=[mac(receiver)+mac(sender)+b'\x88\xb5'+nonce+bytes([i])*30 for i in range(4)]
received=set();wire_received=set();down=False
with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as tx,socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as rx:
    tx.bind((sender,0));rx.bind((receiver,0));rx.setblocking(False)
    for frame in frames:tx.send(frame);time.sleep(.05)
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        for source in select.select([wire]+([] if down else [rx]),[],[],.1)[0]:
            try:frame,addr=source.recvfrom(65536)
            except OSError as e:
                if source is rx and e.errno==errno.ENETDOWN and 'UP' not in links[receiver]['flags']:
                    down=True;continue
                raise
            if addr[2]==socket.PACKET_OUTGOING:continue
            if source is wire:
                decoded=decode_otmh_ssp(frame,{5,13})
                if decoded and decoded[0]==({'fv4001':5,'fv4002':13}[receiver]) and nonce in decoded[1]:
                    wire_received.add(decoded[1].split(nonce)[1][0])
            elif frame in frames:received.add(frames.index(frame))
wire.close()
print(json.dumps({'sender':sender,'receiver':receiver,'sent':len(frames),'received':len(received),
                  'wire_received':len(wire_received),'receiver_admin_down':down,'sequences':sorted(received)}))
