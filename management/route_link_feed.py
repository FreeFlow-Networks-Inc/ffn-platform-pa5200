#!/usr/bin/env python3
"""MP-owned route link observations through controld and the selected backend."""
import json
import sys
import time
import uuid
sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
from ffn_controld_client import ControldClient


def request(action,payload):
    result=ControldClient().plane_request(dict(v=1,id=str(uuid.uuid4()),resource='network',action=action,payload=payload))
    if not result.get('ok'): raise RuntimeError('Route link refresh not acknowledged')
    return result['result']


def main():
    while True:
        try:
            state=request('status',{})
            request('apply',dict(revision=state['config']['revision'],refresh_route_links=True))
        except Exception as error: print(str(error),flush=True)
        time.sleep(5)


if __name__=='__main__':main()
