#!/usr/bin/env python3
"""Read or renegotiate a mapped copper port through the MP control daemon."""
import argparse
import json
import socket
import uuid


def call(action,payload):
    request={'v':1,'id':str(uuid.uuid4()),'resource':'faceplate','action':action,'payload':payload}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(125);conn.connect('/run/ffn-plane-mp/control.sock')
        conn.sendall(json.dumps(request).encode()+b'\n');data=bytearray()
        while not data.endswith(b'\n'):
            chunk=conn.recv(65536)
            if not chunk or len(data)+len(chunk)>1024*1024:raise RuntimeError('MP reply incomplete; request ID '+request['id'])
            data.extend(chunk)
    reply=json.loads(data)
    if not reply.get('ok'):raise RuntimeError('MP request '+request['id']+': '+str(reply.get('error')))
    return reply['result']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','renegotiate','recover-pairs'))
    parser.add_argument('--port',type=int,choices=range(1,5),required=True)
    args=parser.parse_args()
    state=call('status',{})
    if args.action!='status':
        result=call('apply',{'revision':state['revision'],'port':args.port,('restart_autoneg' if args.action=='renegotiate' else 'restore_pair_map'):True})
        print(json.dumps(result,indent=2))
    else:
        print(json.dumps(next(p for p in state['ports'] if p['port']==args.port),indent=2))


if __name__=='__main__':main()
