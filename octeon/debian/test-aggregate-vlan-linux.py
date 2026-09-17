#!/usr/bin/env python3
"""Exercise owned VLANs and management profiles in disposable namespaces."""
import json
import subprocess
import uuid
import ffn_aggregate_vlan as vlan


def run(*args,**kw):return subprocess.run(args,check=True,text=True,capture_output=True,timeout=15,**kw).stdout


def main():
    ident=uuid.uuid4().hex[:8];client='ffn-vlan-'+ident;peer='ffn-peer-'+ident;outside='ffn-out-'+ident;names=[];token=str(uuid.uuid4())
    unit=dict(name='ae1.69',tag=69,mtu=1500,addresses=['192.0.2.1/24'],management=dict(profile='ping',ping=True,tcp=[],udp=[],sources=[]))
    network=dict(enabled=False,addresses=[],mtu=1500,units=[unit])
    ip=lambda *args:run('ip','-n',client,*args)
    def ping(address,success=True):
        result=subprocess.run(['ip','netns','exec',peer,'ping','-c','1','-W','1',address],capture_output=True,text=True,timeout=4)
        assert (result.returncode==0)==success,result.stdout+result.stderr
    try:
        for name in (client,peer,outside):run('ip','netns','add',name);names.append(name);run('ip','-n',name,'link','set','lo','up')
        ip('link','add','ae1','type','veth','peer','name','peer0','netns',peer)
        ip('link','set','ae1','alias','ffn-aggregate:'+token)
        run('ip','netns','exec',client,'nft','-f','-',input=vlan.guard('ae1'))
        vlan.reconcile(client,'ae1',token,network,ip,run);ip('link','set','ae1','up')
        run('ip','-n',peer,'link','set','peer0','up')
        run('ip','-n',peer,'link','add','link','peer0','name','p69','type','vlan','id','69')
        run('ip','-n',peer,'address','add','192.0.2.2/24','dev','p69');run('ip','-n',peer,'link','set','p69','up')
        ping('192.0.2.1')
        ip('link','add','out0','type','veth','peer','name','in0','netns',outside)
        ip('address','add','198.51.100.1/24','dev','out0');ip('link','set','out0','up')
        run('ip','-n',outside,'address','add','198.51.100.2/24','dev','in0');run('ip','-n',outside,'link','set','in0','up')
        run('ip','-n',outside,'route','add','192.0.2.0/24','via','198.51.100.1')
        run('ip','-n',peer,'route','add','198.51.100.0/24','via','192.0.2.1')
        run('ip','netns','exec',client,'sysctl','-q','-w','net.ipv4.ip_forward=1')
        ping('198.51.100.2',False)
        run('ip','netns','exec',client,'nft','delete','table','inet','ffn_aggregate_ae1');ping('198.51.100.2')
        run('ip','netns','exec',client,'nft','-f','-',input=vlan.guard('ae1'));ping('198.51.100.2',False)
        index=json.loads(ip('-j','link','show','ae1'))[0]['ifindex']
        child=json.loads(ip('-j','link','show','ae1.69'))[0]['ifindex']
        unit['management']['ping']=False;vlan.reconcile(client,'ae1',token,network,ip,run);ping('192.0.2.1',False)
        unit['management']['ping']=True;unit['addresses']=['192.0.2.3/24'];vlan.reconcile(client,'ae1',token,network,ip,run)
        ping('192.0.2.3');ping('192.0.2.1',False)
        assert json.loads(ip('-j','link','show','ae1'))[0]['ifindex']==index
        assert json.loads(ip('-j','link','show','ae1.69'))[0]['ifindex']==child
        # A foreign untagged network must not answer ARP for a VLAN address.
        run('ip','-n',peer,'address','flush','dev','p69')
        run('ip','-n',peer,'address','add','192.0.2.2/24','dev','peer0');ping('192.0.2.3',False)
        vlan.reconcile(client,'ae1',token,dict(network,units=[]),ip,run)
        assert all(l['ifname']!='ae1.69' for l in json.loads(ip('-j','link')))
        print(json.dumps(dict(vlan_ping=True,profile_deny=True,transit_guard=True,address_update=True,stable_parent=True,stable_child=True,untagged_isolation=True,removed=True)))
    finally:
        for name in reversed(names):run('ip','netns','delete',name)

if __name__=='__main__':main()
