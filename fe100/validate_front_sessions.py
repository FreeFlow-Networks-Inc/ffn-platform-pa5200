#!/usr/bin/env python3
"""Isolated FE100 front egress with nonce-tagged DP capture on the 5/13 DAC.

Default tests same-port FE forwarding. --front5 reverses the tuple and direction.
Experimental --cross sends to the other front port and installs an exact-match
BCM capture rule for its DAC return. A single run proves only one direction;
it does not qualify production policy, simultaneous traffic or other ports.
"""
import argparse
import json
import re
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from validate_physical_sessions import DMAC, qualifies, checksum


def aggregate_owners(root=Path('/var/lib/ffn-ngfw/aggregate-runtime'),now=None):
    """Lab tests abort on production owner churn; never continue after recovery."""
    now=time.monotonic() if now is None else now;owners={}
    for path in root.glob('ae*-status.json'):
        row=json.loads(path.read_text())
        if row.get('state') in ('stopped','stopping'):continue
        if (row.get('state')!='active' or not row.get('applied') or row.get('error') or
            not 0<=now-row.get('updated_monotonic',-1)<=5):
            raise RuntimeError('Production aggregate is not stable: '+path.name)
        owners[path.name]={k:row[k] for k in ('token','pid','process_start','boot_id')}
    return owners


def directional_frames(token,count,front5=False,nat=None,protocol='udp'):
    from ffn_fe100_nat_lab import probe_frames
    result=probe_frames(token,count,protocol)
    if nat:
        from ffn_fe100_nat_lab import tuples,rewrite
        original,_=tuples(nat,front5)
        return [rewrite(f,original) for f in result]
    if not front5:return result
    # Swapping source/destination words preserves IP and TCP/UDP one's-complement
    # sums; ports swap as well. Tests verify both resulting checksums.
    return [f[:26]+f[30:34]+f[26:30]+f[36:38]+f[34:36]+f[38:] for f in result]


def vlan_return_frame(frame):
    raw=bytearray(frame);raw[22]-=1
    raw[24:26]=bytes(2);raw[24:26]=struct.pack('!H',checksum(bytes(raw[14:34])))
    return DMAC+raw[6:12]+bytes.fromhex('81000fa0')+raw[12:]


def qualifies_front(phases):
    return qualifies(phases) and all(not p.get('unexpected_dp_packets') and not p.get('capture_drops')
                                    and not p.get('dp_capture_drops') for p in phases.values())


def capture_buffer(sock):
    # The shared packet trunk also carries production traffic. A tiny default
    # AF_PACKET queue can drop lab packets while a probe is transmitting.
    # Scope the larger buffer to this root-owned socket, not a global sysctl.
    sock.setsockopt(socket.SOL_SOCKET,33,8*1024*1024) # Linux SO_RCVBUFFORCE


def capture_drops(sock):
    # Linux SOL_PACKET / PACKET_STATISTICS; reading resets this socket's stats.
    return struct.unpack('=II',sock.getsockopt(263,6,8))[1]


def expected_return(token,count,front5=False,nat=None,protocol='udp'):
    packets=directional_frames(token,count,front5,nat,protocol)
    if nat:
        from ffn_fe100_nat_lab import tuples,rewrite
        _,translated=tuples(nat,front5)
        packets=[rewrite(f,translated) for f in packets]
    return [vlan_return_frame(f) for f in packets]


def capture_return(sock,argv,token,count,front5,nat=None,protocol='udp'):
    expected=expected_return(token,count,front5,nat,protocol)
    while select.select([sock],[],[],0)[0]:sock.recv(65536)
    capture_drops(sock)
    process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    packets=[];deadline=time.monotonic()+15
    try:
        while time.monotonic()<deadline:
            if select.select([sock],[],[],.1)[0]:
                raw,addr=sock.recvfrom(65536)
                if addr[2]==socket.PACKET_OUTGOING or bytes.fromhex(token) not in raw:continue
                item={'raw':raw.hex(),'original':[],'rewritten':[]}
                # VM pcs/packets/fe100.py fe100ToOcteonHdr: 32-byte CMH,
                # ingress pport in bits27:22 of the word at byte24.
                if len(raw)>=32:
                    port=(int.from_bytes(raw[24:28],'big')>>22)&63
                    item['received_port']=port
                    if port==(5 if front5 else 13):
                        item['rewritten']=[i for i,f in enumerate(expected) if raw[32:]==f]
                packets.append(item)
            if process.poll() is not None:break
        out,err=process.communicate(timeout=2)
        if process.returncode:raise RuntimeError('DP probe failed: '+err[-2000:])
        result=json.loads(out)
        result['dp_capture_drops']=result.get('capture_drops',0)
        result['capture_drops']=capture_drops(sock)
        result['unexpected_dp_packets']=result['packets']
        result.update(packets=packets,original=[],rewritten=sorted({i for p in packets for i in p['rewritten']}))
        return result
    finally:
        if process.poll() is None:process.kill();process.communicate()


def probe(token,count,baseline,front5=False,cross=False,nat=None,protocol='udp'):
    from ffn_dp_packet_transport import encode,decode_otmh_ssp,validate_trunk
    validate_trunk('ffnpkt0')
    status=lambda:json.loads(Path('/sys/kernel/debug/ffn_dp_packet_init/status').read_text())
    before=status();expected=directional_frames(token,count,front5,nat,protocol);rewritten=[DMAC+f[6:] for f in expected]
    inject=13 if front5 else 5;ingress=5 if front5 else 13
    found=[]
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as sock:
        capture_buffer(sock)
        sock.bind(('ffnpkt0',0));sock.setblocking(False)
        for frame in expected:
            raw=encode(inject,frame)
            if sock.send(raw)!=len(raw):raise RuntimeError('short TX')
            time.sleep(.03)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if not select.select([sock],[],[],.1)[0]:continue
            raw,addr=sock.recvfrom(65536)
            if addr[2]==socket.PACKET_OUTGOING or bytes.fromhex(token) not in raw:continue
            decoded=decode_otmh_ssp(raw,[5,13]);item={'raw':raw.hex(),'original':[],'rewritten':[]}
            if decoded:
                port,frame=decoded;item['received_port']=port
                if port==(ingress if baseline or cross else inject):
                    item['original']=[i for i,f in enumerate(expected) if frame==f]
                    item['rewritten']=[i for i,f in enumerate(rewritten) if frame==f]
            found.append(item)
        dropped=capture_drops(sock)
    after=status();tx=after['trunk']['tx_completed']-before['trunk']['tx_completed']
    if tx<count or after['trunk']['error'] or after['trunk']['bad_dma']:
        raise RuntimeError('DP packet transport failed')
    return {'token':token,'count':count,'packets':found,'before':before,'after':after,'capture_drops':dropped,
            'tx_completed':tx,'original':sorted({i for p in found for i in p['original']}),
            'rewritten':sorted({i for p in found for i in p['rewritten']})}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--probe');p.add_argument('--baseline',action='store_true')
    p.add_argument('--front5',action='store_true')
    p.add_argument('--cross',action='store_true',help='forward to the other front port; capture its DAC return')
    p.add_argument('--vlan-return',action='store_true',help='experimental tagged FE100 return to MP capture')
    p.add_argument('--nat',choices=('address','port'),help='isolated IPv4 NAT or port translation qualification')
    p.add_argument('--protocol',choices=('udp','tcp'),default='udp',help='isolated rewrite/checksum probe; no TCP state admission')
    p.add_argument('--count',type=int,choices=range(1,5),default=1);a=p.parse_args()
    if a.vlan_return and not a.cross:p.error('--vlan-return requires --cross')
    if a.nat and not a.probe and not (a.cross and a.vlan_return):p.error('--nat requires --cross --vlan-return')
    if a.probe:print(json.dumps(probe(a.probe,a.count,a.baseline,a.front5,a.cross,a.nat,a.protocol)));return
    if subprocess.run(['systemctl','is-active','--quiet','ffn-fabric.service']).returncode==0:
        raise RuntimeError('software fabric must be stopped')
    report={'schema':1,'scope':'front5 FE100 egress -> DAC -> front13 DP capture' if a.front5 else 'front13 FE100 egress -> DAC -> front5 DP capture',
            'bidirectional_distinct_ports_verified':False,'phases':{},'cleanup_errors':[],
            'session_offload_verified':False}
    ingress=5 if a.front5 else 13
    egress=18-ingress if a.cross else ingress
    report.update(ingress=ingress,egress=egress,nat_mode=a.nat,protocol=a.protocol,nat_direction_verified=False,
                  production_nat_qualified=False,distinct_port_direction_verified=False)
    report['scope']=f'front{ingress} -> FE100 -> front{egress} -> DAC -> DP capture'
    if a.vlan_return:report['scope']=f'front{ingress} -> FE100 -> front{egress} VLAN4000 -> DAC -> front{ingress} -> FE100 -> MP capture'
    path=Path('/var/log')/('ffn-front-session-'+str(time.time_ns())+'.json')
    save=lambda:path.write_text(json.dumps(report,indent=2))
    report['production_owners']=aggregate_owners();save()
    def production_check():
        if aggregate_owners()!=report['production_owners']:
            raise RuntimeError('Production aggregate lifetime changed; isolated lab aborted')
    def route(mode,ids=None):
        result=subprocess.run(['/usr/local/sbin/ffn-cp',
            'python3 /usr/local/sbin/ffn_fe100_bcm_lab.py'],
            input=json.dumps({'mode':mode,'ids':ids or {}}),capture_output=True,text=True,timeout=30)
        if result.returncode:raise RuntimeError('BCM lab operation failed: '+result.stdout[-3000:]+result.stderr[-1000:])
        r=json.loads(result.stdout)
        report.setdefault('bcm',[]).append(r);save()
        if not r.get('completed') or any('FFN_FAIL' in s for s in r.get('markers',[])):
            raise RuntimeError('BCM route failed')
        return r
    ld='/usr/local/lib64:/usr/local/lib64/3p:/usr/local/lib/ffn/owner-deps'
    err=tempfile.TemporaryFile(mode='w+')
    cp=subprocess.Popen(['/usr/local/sbin/ffn-cp','env LD_LIBRARY_PATH='+ld+
        ' LD_PRELOAD=/usr/lib/mips64-linux-gnuabi64/libsqlite3.so.0 python3 /usr/local/sbin/ffn_fe100_packet_lab.py --serve '+('--front5' if a.front5 else '--front13')+(' --cross' if a.cross else '')+(' --vlan-return' if a.vlan_return else '')+(' --nat '+a.nat if a.nat else '')+' --protocol '+a.protocol],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True)
    def response():
        if not select.select([cp.stdout],[],[],55)[0]:raise TimeoutError('CP timeout')
        line=cp.stdout.readline()
        if not line:err.seek(0);raise RuntimeError(err.read()[-3000:])
        return json.loads(line)
    def command(op):
        cp.stdin.write(json.dumps({'op':op})+'\n');cp.stdin.flush()
        r=response();report.setdefault('cp',[]).append(r);save()
        if r.get('restored'):report['cp_cleanup']=r
        if not r.get('ok'):raise RuntimeError('CP operation failed: '+str(r))
        return r['registers']
    redirected=False;rules=[];capture=None;feature=False;link_raised=False;baseline_started=False
    def create(mode):
        result=route(mode)
        markers='\n'.join(result.get('markers',[]))
        rule={k:int(v) for k,v in re.findall(r'\b(group|entry|stat|dq1|dq2|presel|trap)=(-?\d+)',markers)}
        rules.append(rule);report['rules']=rules;save()
        if not {'group','entry'}<=set(rule):raise RuntimeError('rule IDs missing; inspect BCM journal')
    try:
        report['controller']=response();save()
        if report['controller'].get('ready') is not True:
            report['cp_cleanup']=report['controller'] if report['controller'].get('restored') else response()
            raise RuntimeError('CP hardware prerequisites not satisfied; no packet test was started: '+
                               '; '.join(report['controller'].get('blockers',[])))
        report['queue_preparation']=route('queues-prepare');save()
        production_check()
        baseline_started=True
        report['baseline_routes']=route('baseline-begin');save()
        if a.vlan_return:
            link=json.loads(subprocess.check_output(['ip','-j','link','show','dev','enp8s0f1'],text=True))[0]
            report['capture_link_before']=link;save()
            if 'UP' not in link['flags']:
                link_raised=True;subprocess.run(['ip','link','set','dev','enp8s0f1','up'],check=True)
            features=subprocess.check_output(['ethtool','-k','enp8s0f1'],text=True)
            if 'rx-all: off' not in features:raise RuntimeError('capture rx-all baseline must be off')
            feature=True;subprocess.run(['ethtool','-K','enp8s0f1','rx-all','on'],check=True)
            capture=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
            capture_buffer(capture)
            capture.bind(('enp8s0f1',0));capture.setblocking(False)
            capture.setsockopt(263,1,struct.pack('IHH8s',socket.if_nametoindex('enp8s0f1'),1,0,bytes(8)))
        for phase,op in [('baseline','snapshot'),('miss','prepare'),('hit','install'),('drop','drop'),('removed','remove')]:
            production_check()
            if phase=='miss':
                create(f'dsa-front{egress}-create')
                if a.cross and not a.vlan_return:
                    create(f'cross{ingress}-input-create')
                    create(f'cross{ingress}-return-create')
                redirected=True
                route(f'cross{ingress}-release' if a.cross and not a.vlan_return else ('front5-session-enable' if a.front5 else 'session-path-enable'))
            before=command(op)
            production_check()
            token=uuid.uuid4().hex
            argv=['python3','/tmp/ffn-dp-ssh.py','python3','/usr/local/sbin/validate_front_sessions.py',
                  '--probe',token,'--count',str(a.count)]
            if phase=='baseline':argv+=['--baseline']
            if a.front5:argv+=['--front5']
            if a.cross:argv+=['--cross']
            if a.nat:argv+=['--nat',a.nat]
            argv+=['--protocol',a.protocol]
            if capture is not None and phase!='baseline':
                result=capture_return(capture,argv,token,a.count,a.front5,a.nat,a.protocol)
            else:
                r=subprocess.run(argv,capture_output=True,text=True,timeout=20)
                if r.returncode:raise RuntimeError('DP probe failed: '+r.stderr[-2000:])
                result=json.loads(r.stdout)
            report['phases'][phase]=result
            production_check()
            after=command('snapshot')
            result['counter_delta']={k:v['raw']-before[k]['raw'] for k,v in after.items()
                                    if k.endswith('_no_rd_clr') and k in before}
            save();print(json.dumps({'phase':phase,'original':result['original'],
                'rewritten':result['rewritten'],'received':len(result['packets']),'report':str(path)}),flush=True)
            if phase=='baseline' and result['original']!=list(range(a.count)):
                raise RuntimeError('physical baseline failed')
        report['session_offload_verified']=qualifies_front(report['phases'])
    except BaseException as e:report['error']=str(e);raise
    finally:
        if redirected:
            try:route(f'cross{ingress}-restore' if a.cross and not a.vlan_return else ('front5-session-restore' if a.front5 else 'session-path-restore'))
            except Exception as e:report['cleanup_errors'].append(str(e))
        for rule in reversed(rules):
            try:route('offload-rule-delete',rule)
            except Exception as e:report['cleanup_errors'].append(str(e))
        if baseline_started:
            try:report['baseline_cleanup']=route('baseline-end')
            except Exception as e:report['cleanup_errors'].append(str(e))
        try:
            if cp.poll() is None and not report.get('cp_cleanup',{}).get('restored'):
                cp.stdin.write('{"op":"finish"}\n');cp.stdin.flush();report['cp_cleanup']=response()
            cp.wait(timeout=30)
            if not report.get('cp_cleanup',{}).get('restored'):raise RuntimeError('CP cleanup not verified')
        except Exception as e:report['cleanup_errors'].append(str(e))
        if capture is not None:capture.close()
        for enabled,argv in ((feature,['ethtool','-K','enp8s0f1','rx-all','off']),
                             (link_raised,['ip','link','set','dev','enp8s0f1','down'])):
            if enabled:
                try:subprocess.run(argv,check=True,capture_output=True)
                except Exception as e:report['cleanup_errors'].append(str(e))
        report['session_offload_verified'] &= not bool(report['cleanup_errors'])
        report['distinct_port_direction_verified']=a.cross and report['session_offload_verified']
        report['nat_direction_verified']=bool(a.nat) and report['session_offload_verified']
        save();print(json.dumps({'verified':report['session_offload_verified'],
            'cleanup_errors':report['cleanup_errors'],'report':str(path)}),flush=True)
    raise SystemExit(0 if report['session_offload_verified'] else 2)


if __name__=='__main__':main()
