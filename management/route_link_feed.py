#!/usr/bin/env python3
"""MP-owned route link observations through controld and the selected backend."""
import json
import sys
import time
import uuid
sys.path[:0]=['/opt/ffn-ngfw-v2','/opt/ffn-ngfw']
from ffn_controld_client import ControldClient


def request():
    result=ControldClient().plane_request(dict(v=1,id=str(uuid.uuid4()),resource='route-links',action='refresh',payload={}))
    if not result.get('ok'): raise RuntimeError('Route link refresh not acknowledged: '+str(result.get('error','unknown error')))
    return result['result']


def main():
    while True:
        try:
            request()
        except Exception as error: print(str(error),flush=True)
        time.sleep(5)


if __name__=='__main__':main()
