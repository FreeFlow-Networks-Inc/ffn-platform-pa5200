#!/usr/bin/env python3
"""Root-only Linux bridge/VRF forwarding tests in a disposable VM namespace."""
import copy
import json
import os
import select
import struct
import subprocess
import tempfile
import time
from pathlib import Path
import ffn_network as network
from ffn_vif_runtime import Linux,Owner

network.NS='ffn-vif-test-'+str(os.getpid())
backend=Linux();handles=[];report={}
def ip(*args):return network.ip(*args)
def checksum(data):
    if len(data)%2:data+=b'\0'
    total=sum(struct.unpack('!%dH'%(len(data)//2),data))
    while total>>16:total=(total&65535)+(total>>16)
    return (~total)&65535
def read(fd,nonce):
    for _ in range(30):
        if not select.select([fd],[],[],.1)[0]:continue
        frame=os.read(fd,65536)
        if nonce in frame:return frame
    raise AssertionError('kernel did not forward test frame')
try:
    network.run('ip','netns','add',network.NS);ip('link','set','lo','up')
    ip('link','add','br-data','type','bridge','vlan_filtering','1','vlan_default_pvid','0','stp_state','0')
    ip('link','set','br-data','up')
    with tempfile.TemporaryDirectory() as tmp:
        network.STATE=Path(tmp)/'network.json'
        network.REQUIRE_ATTACHMENT=True
        owner=Owner(backend,{5,13},Path(tmp)/'vifs.json',Path(tmp)/'intent')
        vifs={n:{'port':p,'vlan':100,'enabled':True,'network':{'mode':'l2','vlans':[100],'pvid':100}}
              for n,p in [('fv3001',5),('fv3002',13)]}
        owner.replace({'revision':0,'vifs':vifs})
        handles=[backend.open(n) for n in vifs]
        time.sleep(.5) # bridge carrier work runs asynchronously after TAP attach
        nonce=b'FFN-VIF-BRIDGE-VALIDATION'
        frame=bytes.fromhex('ffffffffffff02ff0000000188b5')+nonce+bytes(30)
        os.write(handles[0],frame);assert read(handles[1],nonce)==frame
        report['l2_bridge_forwarding']=True
        network.run('ip','netns','exec',network.NS,'bridge','vlan','add','dev','fv3001','vid','101')
        try:backend.verify(owner.config)
        except RuntimeError:report['bridge_drift_rejected']=True
        else:raise AssertionError('bridge drift accepted')
        for fd in handles:os.close(fd)
        handles=[]
        network.create_vrf('vrf-viftest',1001)
        for n,subnet in [('fv3001',1),('fv3002',2)]:
            vifs[n]['network']={'mode':'l3','addresses':['198.18.%d.1/24'%subnet],'vrf':'vrf-viftest'}
        owner.replace({'revision':1,'vifs':vifs})
        handles=[backend.open(n) for n in vifs]
        for path,value in [('/proc/sys/net/ipv4/ip_forward','1'),('/proc/sys/net/ipv4/conf/all/rp_filter','0')]:
            network.run('ip','netns','exec',network.NS,'python3','-c',
                        'from pathlib import Path;import sys;Path(sys.argv[1]).write_text(sys.argv[2])',path,value)
        mac=bytes.fromhex(backend.links()['fv3001']['address'].replace(':',''))
        ip('neigh','replace','198.18.2.2','lladdr','02:ff:00:00:00:02','dev','fv3002','nud','permanent')
        nonce=b'FFN-VIF-VRF-ROUTING';udp=struct.pack('!HHHH',45001,45002,8+len(nonce),0)+nonce
        header=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),1,0,64,17,0,bytes([198,18,1,2]),bytes([198,18,2,2]))
        header=header[:10]+struct.pack('!H',checksum(header))+header[12:]
        frame=mac+bytes.fromhex('02ff00000001')+b'\x08\x00'+header+udp
        os.write(handles[0],frame);received=read(handles[1],nonce)
        assert received[22]==63 and checksum(received[14:34])==0 and received[:6]==bytes.fromhex('02ff00000002')
        report['l3_vrf_ttl_checksum_forwarding']=True
        cfg={'revision':0,'ports':{},'vrfs':{'vrf-viftest':1001},'routes':[],'rules':[]}
        network.save(cfg)
        route={'dst':'198.19.2.0/24','dev':'fv3002','via':'198.18.2.2','table':1001}
        rule={'from':'198.18.1.0/24','iif':'fv3001','table':1001,'priority':101}
        cfg=network.patch(cfg,{'revision':0,'routes':[route],'rules':[rule]})['config']
        header=header[:16]+bytes([198,19,2,2])
        header=header[:10]+b'\0\0'+header[12:]
        header=header[:10]+struct.pack('!H',checksum(header))+header[12:]
        os.write(handles[0],frame[:14]+header+udp);received=read(handles[1],nonce)
        assert received[22]==63 and checksum(received[14:34])==0 and received[:6]==bytes.fromhex('02ff00000002')
        report['vif_static_route_and_policy_forwarding']=True
        changed=copy.deepcopy(owner.config);changed['vifs']['fv3001']['vlan']=200
        try:backend.preflight(owner.config,changed)
        except RuntimeError:report['dependent_policy_reassignment_rejected']=True
        else:raise AssertionError('dependent VIF policy was orphaned')
        cfg=network.patch(cfg,{'revision':cfg['revision'],'routes':[],'rules':[]})['config']
        ip('link','set','fv3001','nomaster')
        try:backend.verify(owner.config)
        except RuntimeError:report['vrf_drift_rejected']=True
        else:raise AssertionError('VRF drift accepted')
finally:
    for fd in handles:os.close(fd)
    subprocess.run(['ip','netns','del',network.NS],check=True)
print(json.dumps(report))
