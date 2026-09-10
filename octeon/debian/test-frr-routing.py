#!/usr/bin/env python3
"""DP-only BGP/OSPF lab using two disposable network namespaces.

No physical interfaces or production routing configuration are changed.
Each daemon uses private UNIX sockets and disables its TCP VTY listener.
"""
import json
import os
from pathlib import Path
import subprocess as S
import tempfile
import time

PREFIX = Path('/usr/local/ffn-router')


def run(*args):
    try:
        return S.check_output(args, stderr=S.STDOUT, text=True)
    except S.CalledProcessError as error:
        raise RuntimeError(str(args)+': '+error.output) from error


def main():
    names = ['ffn-frr-a', 'ffn-frr-b']
    existing = {n['name'] for n in json.loads(run('ip','-j','netns','list'))}
    assert not existing.intersection(names), 'lab namespace already exists'
    root = Path(tempfile.mkdtemp(prefix='ffn-frr-lab-',dir='/var/log'))
    env = dict(os.environ, LD_LIBRARY_PATH=str(PREFIX/'lib'))
    processes, logs, created = [], [], []

    def spawn(index, daemon, config):
        directory = root/str(index); directory.mkdir(exist_ok=True)
        config_path = directory/(daemon+'.conf'); config_path.write_text(config)
        logfile = (directory/(daemon+'.log')).open('w'); logs.append(logfile)
        argv = ['ip','netns','exec',names[index],str(PREFIX/'sbin'/daemon),
                '-f',str(config_path),'-i',str(directory/(daemon+'.pid')),
                '-z',str(directory/'zserv.api'),'--vty_socket',str(directory),
                '-u','root','-g','root','-P','0','--log','stdout']
        process = S.Popen(argv,stdout=logfile,stderr=S.STDOUT,env=env)
        processes.append(process)
        return process

    def stop(group):
        for p in group:
            if p.poll() is None: p.terminate()
        for p in group:
            try: p.wait(timeout=10)
            except S.TimeoutExpired: p.kill(); p.wait()

    def verify(protocol):
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('routing daemon exited; logs: '+str(root))
            learned = True
            for i,name in enumerate(names):
                peer = 2-i
                for family,dst in (('-4',f'203.0.113.{peer}/32'),('-6',f'2001:db8:{peer}::1/128')):
                    routes=json.loads(run('ip','-n',name,family,'-j','route','show','exact',dst))
                    learned &= any(r.get('protocol')==protocol for r in routes)
            if learned: break
            time.sleep(2)
        else:
            raise RuntimeError(protocol+' routes did not converge; logs: '+str(root))
        for i,name in enumerate(names):
            peer=2-i
            run('ip','netns','exec',name,'/usr/bin/busybox','ping','-4','-c','3','-W','2','-I',f'203.0.113.{i+1}',f'203.0.113.{peer}')
            run('ip','netns','exec',name,'/usr/bin/busybox','ping','-6','-c','3','-W','2','-I',f'2001:db8:{i+1}::1',f'2001:db8:{peer}::1')
        print(json.dumps({'test':protocol+'-learned-ipv4-ipv6-routes-and-bidirectional-ping',
                          'echo_requests':12,'passed':True}),flush=True)

    try:
        for name in names:
            run('ip','netns','add',name); created.append(name)
        run('ip','-n',names[0],'link','add','peer0','type','veth','peer','name','peer1','netns',names[1])
        for i,name in enumerate(names):
            run('ip','-n',name,'link','set','peer'+str(i),'name','lab0')
            for dev in ('lo','lab0'): run('ip','-n',name,'link','set',dev,'up')
            for address,dev in ((f'10.255.0.{i+1}/30','lab0'),(f'fd52:ffff::{i+1}/64','lab0'),
                                (f'203.0.113.{i+1}/32','lo'),(f'2001:db8:{i+1}::1/128','lo')):
                run('ip','-n',name,'address','add',address,'dev',dev)
            run('ip','netns','exec',name,'python3','-c',
                "from pathlib import Path; [Path(p).write_text('1\\n') for p in "
                "('/proc/sys/net/ipv4/ip_forward','/proc/sys/net/ipv6/conf/all/forwarding')]")
            spawn(i,'zebra','hostname lab'+str(i)+'\n')
        time.sleep(3)
        bgp=[]
        for i in range(2):
            peer=2-i
            cfg=f'''hostname bgp{i}
router bgp {65001+i}
 bgp router-id 203.0.113.{i+1}
 no bgp ebgp-requires-policy
 neighbor 10.255.0.{peer} remote-as {65000+peer}
 neighbor fd52:ffff::{peer} remote-as {65000+peer}
 address-family ipv4 unicast
  network 203.0.113.{i+1}/32
 exit-address-family
 address-family ipv6 unicast
  neighbor fd52:ffff::{peer} activate
  network 2001:db8:{i+1}::1/128
 exit-address-family
'''
            bgp.append(spawn(i,'bgpd',cfg))
        verify('bgp')
        stop(bgp)
        processes[:] = [p for p in processes if p not in bgp]
        time.sleep(3)
        for i in range(2):
            spawn(i,'ospfd',f'''hostname ospf{i}
interface lab0
 ip ospf area 0
 ip ospf network point-to-point
 ip ospf hello-interval 1
 ip ospf dead-interval 4
interface lo
 ip ospf area 0
router ospf
 ospf router-id 203.0.113.{i+1}
''')
            spawn(i,'ospf6d',f'''hostname ospf6{i}
interface lab0
 ipv6 ospf6 area 0.0.0.0
 ipv6 ospf6 network point-to-point
 ipv6 ospf6 hello-interval 1
 ipv6 ospf6 dead-interval 4
interface lo
 ipv6 ospf6 area 0.0.0.0
router ospf6
 ospf6 router-id 203.0.113.{i+1}
''')
        # Linux uses protocol "ospf" for both OSPFv2 and OSPFv3 routes.
        verify('ospf')
        print(json.dumps({'logs':str(root),'passed':True}),flush=True)
    finally:
        stop(processes)
        for f in logs: f.close()
        for name in reversed(created): run('ip','netns','delete',name)


if __name__=='__main__':
    main()
