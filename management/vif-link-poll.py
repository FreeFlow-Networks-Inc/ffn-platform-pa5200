#!/usr/bin/env python3
"""Refresh VIF physical observations through the MP resource owner."""
import json
import socket
import uuid


def poll():
    request={'v':1,'id':str(uuid.uuid4()),'resource':'vifs','action':'status','payload':{}}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(25)
        conn.connect('/run/ffn-plane-mp/control.sock')
        conn.sendall(json.dumps(request).encode()+b'\n')
        data=bytearray()
        while not data.endswith(b'\n'):
            chunk=conn.recv(65536)
            if not chunk or len(data)+len(chunk)>1024*1024:raise RuntimeError('invalid MP VIF reply')
            data.extend(chunk)
    reply=json.loads(data)
    if not reply.get('ok'):raise RuntimeError('MP VIF observation failed')
    error=reply.get('result',{}).get('link_observation_error')
    if error:raise RuntimeError(error)


if __name__=='__main__':poll()
