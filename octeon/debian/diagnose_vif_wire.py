#!/usr/bin/env python3
"""Four-frame readback of the commissioned 5/13 DAC path, no switch changes."""
import fcntl,json,select,socket,struct,time,uuid
from ffn_dp_packet_transport import encode
result=[]
with open('/run/ffn-fabric.lock','a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as rx,socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as tx:
        rx.bind(('ffnpkt0',0));tx.bind(('ffnpkt0',0));rx.setblocking(False)
        for port in (5,13):
            for vlan in (None,3901):
                nonce=uuid.uuid4().bytes
                frame=bytes.fromhex('02ff0000000202ff00000001')+(b'' if vlan is None else struct.pack('!HH',0x8100,vlan))+b'\x88\xb5'+nonce+bytes(46)
                packet=encode(port,frame);tx.send(packet);found=[];deadline=time.monotonic()+1.5
                while time.monotonic()<deadline:
                    if not select.select([rx],[],[],.1)[0]:continue
                    raw,addr=rx.recvfrom(65536)
                    if nonce in raw and addr[2]!=socket.PACKET_OUTGOING:found.append({'length':len(raw),'prefix':raw[:48].hex(),'packet_type':addr[2]})
                result.append({'port':port,'vlan':vlan,'received':found})
print(json.dumps(result))
