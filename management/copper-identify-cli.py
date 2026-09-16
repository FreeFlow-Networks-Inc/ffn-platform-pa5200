#!/usr/bin/env python3
"""Identify copper panel ports through the MP owner, also used for diagnostics."""
import argparse
import json
import socket
import uuid


def call(action,payload):
    request={'v':1,'id':str(uuid.uuid4()),'resource':'copper-identify','action':action,'payload':payload}
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
    parser.add_argument('action',choices=('status','begin','confirm','cancel'))
    parser.add_argument('--port',type=int,choices=range(1,5))
    parser.add_argument('--token',help='token displayed by begin/status; required for confirm/cancel')
    args=parser.parse_args()
    if (args.action=='begin')!=(args.port is not None):parser.error('--port is required only for begin')
    if (args.action in ('confirm','cancel'))!=(args.token is not None):parser.error('--token is required only for confirm/cancel')
    state=call('status',{})
    if args.action!='status':
        request={'revision':state['config']['revision'],'operation':args.action}
        request.update(port=args.port) if args.action=='begin' else request.update(token=args.token)
        state=call('apply',request)
    print(json.dumps(state,indent=2))


if __name__=='__main__':main()
