#!/usr/bin/env python3
"""Exercise the observer on real DP netlink in a disposable loopback namespace.

The Security context is an explicit test fixture. No production context is
acknowledged, and no CP report, policy, journal or hardware table is modified.
"""
import argparse
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0,'/usr/local/lib/ffn')
import ffn_session_stream_dp as dp
from session_stream import Receiver,local_identity


def inside():
    namespace=subprocess.run(['ip','netns','identify'],text=True,capture_output=True,check=True).stdout.strip()
    if not re.fullmatch(r'ffn-stream-test-[0-9a-f]{8}',namespace):
        raise RuntimeError('Disposable validation namespace required')
    for key in ('nf_conntrack_events','nf_conntrack_acct'):
        Path('/proc/sys/net/netfilter/'+key).write_text('1\n')
    subprocess.run(['ip','link','set','lo','up'],check=True)
    # nft's label literals are bit indices: bit0 is the owned marker and
    # bits1/4 encode grant token9. This matches the core Security renderer.
    subprocess.run(['nft','-f','-'],input='table inet ffn_stream_test {\n chain out {\n type filter hook output priority -150; policy accept;\n ct label set 0 | 1 | 4\n }\n}\n',text=True,check=True)
    nonce=str(uuid.uuid4());receiver=Receiver(nonce);ready=threading.Event();seen=threading.Event();failure=[];frames=[]
    state=dict(revision=1,digest='a'*64,nat={'digest':'b'*64},bindings={})
    collector=dict(local_identity(),reconciliations=1)
    rules={9:dict(interface_pairs=[['fixture-in','fixture-out']],inspection_required=False)}
    stop=threading.Event()
    def emit(value):
        receiver.accept(value);frames.append(value)
        if value['operation']=='synchronized':ready.set()
        if value['operation']=='upsert':seen.set()
    def observe():
        try:dp.stream(nonce,emit,stop=stop.is_set)
        except Exception as error:failure.append(error)
    with patch.object(dp.feed,'context',return_value=(state,collector,rules)), \
         patch.object(dp.feed.runtime,'saved',return_value=state), \
         patch.object(dp.feed.runtime,'status',return_value={}), \
         patch.object(dp.feed,'acknowledgement',return_value=collector):
        thread=threading.Thread(target=observe,daemon=True);thread.start()
        try:
            if not ready.wait(6):raise RuntimeError('Initial kernel snapshot failed: '+repr(failure))
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as server:
                client.bind(('127.0.0.1',0));server.bind(('127.0.0.1',0));client.settimeout(2);server.settimeout(2)
                client.sendto(b'ffn-stream-test',server.getsockname());body,peer=server.recvfrom(128)
                server.sendto(body,peer);client.recvfrom(128)
                if not seen.wait(6):
                    with dp.subscribe() as source:current,_=dp.snapshot(source)
                    raise RuntimeError('Owned UDP update absent: '+repr(failure)+' kernel='+repr(current))
                rows=[f['payload'] for f in frames if f['operation']=='upsert']
                if not any(r['original'].get('source_port')==client.getsockname()[1] and
                           r['reply'].get('source_port')==server.getsockname()[1] for r in rows):
                    raise RuntimeError('Exported kernel original/reply ports do not match the test sockets')
                subprocess.run(['ip','route','add','blackhole','198.18.0.0/15'],check=True)
                thread.join(6)
                if thread.is_alive() or not failure or not isinstance(failure[0],dp.EventGap):
                    raise RuntimeError('Route notification did not invalidate the stream: '+repr(failure))
        finally:
            stop.set();thread.join(2)
    return dict(schema=1,policy_source='isolated test fixture',kernel_snapshot=True,
                kernel_session_update=True,route_invalidation=True,ordered_receiver=True,
                messages=len(frames),packets_sent=2,hardware_admission=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--inside',action='store_true');args=parser.parse_args()
    if args.inside:
        print(json.dumps(inside()));raise SystemExit()
    if not args.run or os.geteuid()!=0:raise SystemExit('Use --run as root for disposable namespace validation')
    name='ffn-stream-test-'+uuid.uuid4().hex[:8]
    subprocess.run(['ip','netns','add',name],check=True)
    try:
        result=subprocess.run(['ip','netns','exec',name,sys.executable,str(Path(__file__).resolve()),'--inside'],
                              text=True,capture_output=True,timeout=25)
        print(result.stdout,end='');print(result.stderr,file=sys.stderr,end='');result.check_returncode()
    finally:subprocess.run(['ip','netns','delete',name],check=True)
