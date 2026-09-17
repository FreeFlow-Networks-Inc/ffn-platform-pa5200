#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Isolated FFN userspace LACP versus Linux bonding, with real IP forwarding.

Only newly created namespaces and veth/TAP devices are used. No physical
interface, BCM operation, routing service or existing packet owner is touched.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time

SYSTEM='02:00:00:00:10:01'


def run(*args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=8).stdout


def atomic(path,value):
    temp=path.with_suffix('.new');temp.write_text(json.dumps(value));temp.replace(path)


def worker(directory,activity):
    import fcntl,select,socket,struct
    from ffn_lacp_engine import Engine
    class Gates:
        def apply(self,value):self.state=value;return value
    driver=Gates();sockets={};tap=None;counts={'rx':0,'tx':0,'dropped':0,'lacp_rx':0,'lacp_tx':0}
    def stop(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    try:
        for port in (1,2):
            sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
            sock.bind(('p'+str(port),0));sock.setblocking(False);sockets[port]=sock
        members={p:Path('/sys/class/net/p'+str(p)+'/address').read_text().strip() for p in (1,2)}
        engine=Engine(SYSTEM,101,members,driver,activity=activity,rate='fast')
        tap=os.open('/dev/net/tun',os.O_RDWR|os.O_NONBLOCK)
        request=0x800454ca if platform.machine().startswith('mips') else 0x400454ca
        fcntl.ioctl(tap,request,struct.pack('16sH',b'ffnae0',0x0002|0x1000))
        run('ip','link','set','ffnae0','address',SYSTEM,'up')
        run('ip','addr','add','192.0.2.2/24','dev','ffnae0')
        next_link=next_report=0;deadline=time.monotonic()+85
        while time.monotonic()<deadline:
            now=time.monotonic()
            control=json.loads((directory/'control.json').read_text()) if (directory/'control.json').exists() else {}
            if now>=next_link:
                for port in (1,2):
                    try:link=Path('/sys/class/net/p'+str(port)+'/carrier').read_text().strip()=='1'
                    except OSError as error:
                        if error.errno!=errno.EINVAL:raise
                        link=False
                    engine.link(port,link,10000 if link else 0,now)
                next_link=now+0.5
            engine.tick(now)
            for port,frame in engine.transmissions(now):
                if port not in control.get('mute',[]):
                    try:sockets[port].send(frame);counts['lacp_tx']+=1
                    except OSError as error:
                        if error.errno!=errno.ENETDOWN:raise
                        engine.link(port,False,0,time.monotonic())
            if now>=next_report:
                atomic(directory/'status.json',dict(engine.status(time.monotonic()),traffic=counts));next_report=now+0.2
            readable,_,_=select.select([tap,*sockets.values()],[],[],0.05)
            for source in readable:
                if source==tap:
                    frame=os.read(tap,2048)
                    ports=[p for p,v in driver.state.items() if v['distribute']]
                    if ports:
                        # Stable L2 selection for this test data adapter only.
                        port=ports[sum(frame[:12])%len(ports)]
                        try:sockets[port].send(frame);counts['tx']+=1
                        except OSError as error:
                            if error.errno!=errno.ENETDOWN:raise
                            engine.link(port,False,0,time.monotonic());counts['dropped']+=1
                    else:counts['dropped']+=1
                    continue
                try:frame,address=source.recvfrom(2048)
                except OSError as error:
                    if error.errno not in (errno.ENETDOWN,errno.EAGAIN):raise
                    continue
                if address[2]==socket.PACKET_OUTGOING:continue
                port=next(p for p,s in sockets.items() if s is source)
                if frame[12:14]==b'\x88\x09':
                    if port not in control.get('mute',[]):
                        engine.receive(port,frame,time.monotonic());counts['lacp_rx']+=1
                elif driver.state[port]['collect']:
                    os.write(tap,frame);counts['rx']+=1
                else:counts['dropped']+=1
    except KeyboardInterrupt:pass
    finally:
        if 'engine' in locals():engine.stop(time.monotonic())
        if tap is not None:os.close(tap)
        for sock in sockets.values():sock.close()


def test(activity='active'):
    if os.geteuid()!=0:raise RuntimeError('root required for isolated namespaces')
    names=['ffn-lacp-interop-'+str(os.getpid())+'-'+s for s in ('ffn','linux')]
    made=[];process=None
    with tempfile.TemporaryDirectory(prefix='ffn-lacp-interop-') as temp:
        directory=Path(temp)
        try:
            for name in names:
                run('ip','netns','add',name);made.append(name);run('ip','-n',name,'link','set','lo','up')
            for port in (1,2):
                interface='p'+str(port)
                run('ip','link','add',interface,'netns',names[0],'type','veth','peer','name',interface,'netns',names[1])
                for side,ns in enumerate(names):
                    run('ip','-n',ns,'link','set',interface,'address','02:00:00:00:%02x:%02x'%(port,side+1))
            peer=names[1]
            run('ip','-n',peer,'link','add','bond0','type','bond','mode','802.3ad','miimon','100','lacp_rate','fast','ad_select','stable')
            run('ip','-n',peer,'link','set','bond0','address','02:00:00:00:10:02')
            for port in (1,2):
                run('ip','-n',peer,'link','set','p'+str(port),'master','bond0')
                for ns in names:run('ip','-n',ns,'link','set','p'+str(port),'up')
            run('ip','-n',peer,'link','set','bond0','up')
            run('ip','-n',peer,'addr','add','192.0.2.1/24','dev','bond0')
            log=(directory/'worker.log').open('w')
            process=subprocess.Popen(['ip','netns','exec',names[0],sys.executable,str(Path(__file__).resolve()),
                '--worker',str(directory),'--activity',activity],stdout=log,stderr=log)
            def status():
                if process.poll() is not None:raise RuntimeError('worker stopped: '+(directory/'worker.log').read_text())
                try:return json.loads((directory/'status.json').read_text())
                except FileNotFoundError:return {}
            def wait_for(predicate,seconds=25):
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    value=status()
                    if predicate(value):return value
                    time.sleep(0.15)
                raise RuntimeError('negotiation timeout: '+json.dumps(status())+'\n'+run('ip','netns','exec',peer,'cat','/proc/net/bonding/bond0'))
            def both_negotiated(value):
                if value.get('distributing')!=[1,2]:return False
                bond=run('ip','netns','exec',peer,'cat','/proc/net/bonding/bond0')
                return 'Number of ports: 2' in bond and 'Partner Mac Address: '+SYSTEM in bond
            wait_for(both_negotiated)
            # Linux may still be processing our most recent collection PDU.
            def ping(source=peer,destination='192.0.2.2'):
                deadline=time.monotonic()+5
                while True:
                    try:return run('ip','netns','exec',source,'ping','-n','-c','2','-W','1',destination)
                    except subprocess.CalledProcessError:
                        if time.monotonic()>deadline:raise
            ping();ping(names[0],'192.0.2.1')
            # No carrier change: losing only PDUs must withdraw one member.
            atomic(directory/'control.json',{'mute':[1]})
            wait_for(lambda v:v.get('distributing')==[2],8);ping()
            atomic(directory/'control.json',{'mute':[]})
            wait_for(lambda v:v.get('distributing')==[1,2]);ping()
            run('ip','-n',names[0],'link','set','p1','down')
            wait_for(lambda v:v.get('distributing')==[2],5);ping()
            run('ip','-n',names[0],'link','set','p1','up')
            wait_for(both_negotiated)
            run('ip','-n',names[0],'link','set','p2','down')
            wait_for(lambda v:v.get('distributing')==[1],5);ping();ping(names[0],'192.0.2.1')
            final=status()
            return dict(activity=activity,negotiated_members=2,linux_interoperability=True,ipv4_forwarding=True,
                        pdu_timeout_withdrawal=True,renegotiation=True,member_link_failover=True,
                        each_member_forwarding=True,bidirectional_ip=True,
                        traffic=final['traffic'],physical_ports_used=False)
        finally:
            if process is not None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            if 'log' in locals():log.close()
            for name in reversed(made):run('ip','netns','delete',name)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',type=Path)
    parser.add_argument('--activity',choices=('active','passive'),default='active');args=parser.parse_args()
    if args.worker:worker(args.worker,args.activity)
    else:print(json.dumps(test(args.activity)))
