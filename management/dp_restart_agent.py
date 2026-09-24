#!/usr/bin/env python3
"""Challenge/response relay for the DP's native mailbox recovery agent.

Reports DP boot identity, never the CP's CPU counters or recovery as forwarding.
"""
import sys
import time
import uuid
sys.path.insert(0,'/usr/local/lib/ffn')
from ffn_agent_protocol import LIMIT, parse, frame, canonical
from plane_restart_node import observe


def serve(source=None,sink=None):
    source=source or sys.stdin.buffer;sink=sink or sys.stdout.buffer
    instance=str(uuid.uuid4());sequence=0
    while True:
        raw=source.readline(LIMIT+1)
        if not raw:return
        request=parse(raw)
        if set(request)!={'v','nonce','op'} or type(request['v']) is not int or request['v']!=1 or request['op']!='observe':
            raise ValueError('Invalid observation challenge')
        nonce=canonical(request['nonce']);report=observe('dp');sequence+=1
        report.update(restart_acknowledged=True,observed_at=time.time(),
                      host_resources={'available':False,'reason':'DP resource sampler unavailable in recovery runtime'})
        sink.write(frame({'v':1,'nonce':nonce,'role':'dp','platform':'pa5200','instance':instance,
            'boot_id':report['boot_id'],'sequence':sequence,'observed_at':time.time(),'report':report}));sink.flush()


if __name__=='__main__':serve()
