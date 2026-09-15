#!/usr/bin/env python3
"""Run with unshare -n: real AF_PACKET sockets, datagram stand-ins for TAP FDs."""
import collections
import os
import socket
import struct
import subprocess
import threading
from ffn_dp_packet_transport import pump, encode


def run(*argv): subprocess.run(argv,check=True)


def main():
    if os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise RuntimeError('run in a separate network namespace')
    run('ip','link','add','fe-peer','type','veth','peer','name','dp-trunk')
    for name in ('fe-peer','dp-trunk'):
        run('ip','link','set',name,'mtu','2000','up')
    sockets=[]
    try:
        for name in ('fe-peer','dp-trunk','dp-trunk'):
            sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
            sockets.append(sock); sock.bind((name,0)); sock.settimeout(3)
        peer,rx,tx=sockets
        app,tap=socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM)
        sockets.extend([app,tap]); app.settimeout(3); tap.setblocking(False)
        class Inspector:
            def tick(self): pass
            def allow(self,port,frame): return frame[-1] != 0xee
        errors=[]; counts=collections.Counter()
        def worker():
            try: pump(rx,tx,{5:tap.fileno()},Inspector(),2,counts)
            except BaseException as e: errors.append(e)
        thread=threading.Thread(target=worker); thread.start()
        payload=b'\x02\x52\x20\0\0\x01'+b'\x02\x52\x20\0\0\x02'+b'\x88\xb5'+bytes(46)
        header=bytearray(32); header[0]=16; header[3]=3
        struct.pack_into('!HH',header,24,5<<6,len(payload))
        peer.send(header+payload)
        assert app.recv(2000)==payload
        app.send(payload)
        while True:
            result,addr=peer.recvfrom(2000)
            if addr[2] != socket.PACKET_OUTGOING:
                assert result==encode(5,payload); break
        peer.send(header+payload[:-1]+b'\xee')
        header[3]=15; peer.send(header+payload)
        thread.join(4)
        assert not thread.is_alive() and not errors, errors
        # The namespace kernel can emit IPv6 discovery on the veth. Those
        # ordinary Ethernet frames must also be rejected as invalid envelopes.
        assert counts['rx_p5']==1 and counts['tx_p5']==1 and counts['inspection_drop']==1 and counts['envelope_rejected']>=1,counts
        app.settimeout(.1)
        try:
            app.recv(2000)
            raise AssertionError('unexpected frame reached the TAP consumer')
        except socket.timeout:
            pass
        print('PASS: real AF_PACKET RX/TX, exact bytes, inspection rejection, control-message rejection')
        print(dict(counts))
    finally:
        for sock in sockets: sock.close()


if __name__=='__main__': main()
