#!/usr/bin/env python3
"""MP-controlled FE100 physical session test, using the front5/13 DAC pair.

Deploy this file on MP and DP. MP mode owns the temporary BCM route and
capture NIC rx-all feature; CP packet_lab owns and restores FE100 table state.
DP --transmit only emits four fixed benchmark UDP frames through ffnpkt0.
No software forwarding process participates in the test path.
"""
import argparse
import json
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

DMAC=bytes.fromhex('025220abcdee')


def checksum(data):
    data+=bytes(len(data)%2)
    s=sum(struct.unpack('!%dH'%(len(data)//2),data))
    while s>>16:s=(s&65535)+(s>>16)
    return (~s)&65535


def frames(token, count=4):
    tag=bytes.fromhex(token)
    if len(tag)!=16 or not 1<=count<=4:raise ValueError('invalid probe')
    a=socket.inet_aton('198.18.0.1');b=socket.inet_aton('198.18.0.2')
    result=[]
    for seq in range(count):
        payload=b'FFN-FE100-SESSION:'+tag+bytes([seq])+bytes(range(52))
        udp=struct.pack('!HHHH',49000,49001,8+len(payload),0)+payload
        c=checksum(a+b+struct.pack('!BBH',0,17,len(udp))+udp) or 65535
        udp=udp[:6]+struct.pack('!H',c)+udp[8:]
        ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),seq,0x4000,64,17,0,a,b)
        ip=ip[:10]+struct.pack('!H',checksum(ip))+ip[12:]
        result.append(bytes.fromhex('02ff0000000202ff000000010800')+ip+udp)
    return result


def transmit(token,count):
    from ffn_dp_packet_transport import encode, validate_trunk
    validate_trunk('ffnpkt0')
    path=Path('/sys/kernel/debug/ffn_dp_packet_init/status')
    before=json.loads(path.read_text())
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as s:
        s.bind(('ffnpkt0',0))
        for f in frames(token,count):
            packet=encode(5,f)
            if s.send(packet)!=len(packet):raise RuntimeError('short TX')
            time.sleep(.03)
    time.sleep(.1)
    after=json.loads(path.read_text())
    print(json.dumps({'sent':count,'before':before,'after':after,
        'completed':after['trunk']['tx_completed']-before['trunk']['tx_completed']}))


def capture_phase(sock,count):
    token=uuid.uuid4().hex
    expected=frames(token,count);rewritten=[DMAC+f[6:] for f in expected]
    while select.select([sock],[],[],0)[0]:sock.recv(65536)
    process=subprocess.Popen(['python3','/tmp/ffn-dp-ssh.py','python3',
        '/usr/local/sbin/validate_physical_sessions.py','--transmit',token,'--count',str(count)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    matches=[];deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        if select.select([sock],[],[],.1)[0]:
            data,addr=sock.recvfrom(65536)
            if addr[2]==socket.PACKET_OUTGOING or bytes.fromhex(token) not in data:continue
            item={'raw':data.hex(),'original':[],'rewritten':[]}
            for offset in (0,32):
                for seq,frame in enumerate(expected):
                    if data[offset:offset+len(frame)]==frame:item['original'].append(seq)
                    if data[offset:offset+len(frame)]==rewritten[seq]:item['rewritten'].append(seq)
            matches.append(item)
        if process.poll() is not None and time.monotonic()>deadline-8:break
    try:out,err=process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill();process.communicate();raise RuntimeError('DP sender timeout')
    if process.returncode:raise RuntimeError('DP sender failed: '+err)
    tx=json.loads(out)
    if tx['completed']<count:raise RuntimeError('PKO completion missing')
    return {'token':token,'count':count,'tx':tx,'packets':matches,
            'original':sorted({n for p in matches for n in p['original']}),
            'rewritten':sorted({n for p in matches for n in p['rewritten']})}


def qualifies(phases):
    wanted=list(range(phases['hit']['count']))
    counters = {
        'miss': ('dfp_flow_lkup_miss_sta_ctr_no_rd_clr',),
        'hit': ('dfp_flow_lkup_flow_hit_sta_ctr_no_rd_clr',
                'dfp_flow_ct_sta_ctr_no_rd_clr','fwd_dir_lkup_req_sta_ctr_no_rd_clr',
                'flu_sem_inc_instr_ctr_no_rd_clr'),
        'drop': ('dfp_flow_lkup_flow_hit_sta_ctr_no_rd_clr',
                 'dfp_flow_drop_sta_ctr_no_rd_clr'),
        'removed': ('dfp_flow_lkup_miss_sta_ctr_no_rd_clr',),
    }
    # Multiple miss probes can hit an ASIC-learned identity after the first.
    count_ok = all(phases[p].get('counter_delta',{}).get(k,-1) >=
                   (1 if p in ('miss','removed') else len(wanted))
                   for p,keys in counters.items() for k in keys)
    exact = lambda p,kind: (len(phases[p]['packets']) == len(wanted) and
                           sorted(n for packet in phases[p]['packets'] for n in packet[kind]) == wanted)
    return (count_ok and phases['baseline']['original']==wanted and exact('baseline','original') and
            phases['hit']['rewritten']==wanted and not phases['hit']['original'] and
            exact('hit','rewritten') and
            not any(phases[p]['rewritten'] for p in ('miss','drop','removed')) and
            not phases['drop']['packets'])


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--transmit');ap.add_argument('--count',type=int,choices=range(1,5),default=1)
    args=ap.parse_args()
    if args.transmit:transmit(args.transmit,args.count);return
    sys.path.insert(0,'/tmp')
    from bcmd import call as bcm
    if subprocess.run(['systemctl','is-active','--quiet','ffn-fabric.service']).returncode==0:
        raise RuntimeError('software fabric must be stopped for physical qualification')
    path=Path('/var/log')/('ffn-physical-session-'+str(time.time_ns())+'.json')
    report={'schema':2,'phases':{},'session_offload_verified':False,'cleanup_errors':[],
            'qualification_scope':{'protocol':'IPv4 UDP','ingress_front_port':13,
                'egress':'MP capture through BCM port8','actions':['next-hop DMAC rewrite','drop'],
                'production_activation':False}}
    def save():path.write_text(json.dumps(report,indent=2))
    def route(mode):
        subprocess.run(['python3','/tmp/prepare-forward-test.py',mode,
            '/opt/ffn-cproot-owrt/tmp/bcmcfg/ffn_bcm_forward_test.c'],check=True,stdout=subprocess.DEVNULL)
        result=bcm('cint.run',script='ffn_bcm_forward_test.c')
        report.setdefault('bcm',[]).append(result);save()
        if not result.get('completed') or any('FFN_FAIL' in l for l in result.get('markers',[])):
            raise RuntimeError('BCM recipe failed: '+json.dumps(result))
    ld='/opt/ffn-compat/tmp/dpfs/usr/local/lib64:/opt/ffn-compat/tmp/dpfs/usr/local/lib64/3p:/opt/ffn-compat/tmp/dpfs/usr/lib64'
    stderr=tempfile.TemporaryFile(mode='w+')
    cp=subprocess.Popen(['/usr/local/sbin/ffn-cp','env LD_LIBRARY_PATH='+ld+
        ' python3 /usr/local/sbin/ffn_fe100_packet_lab.py --serve'],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=stderr,text=True)
    def response():
        if not select.select([cp.stdout],[],[],55)[0]:raise TimeoutError('CP test response timeout')
        line=cp.stdout.readline()
        if not line:
            stderr.seek(0);raise RuntimeError('CP test failed: '+stderr.read()[-4000:])
        return json.loads(line)
    def command(op):
        cp.stdin.write(json.dumps({'op':op})+'\n');cp.stdin.flush()
        result=response();report.setdefault('cp',[]).append(result);save()
        if not result.get('ok'):
            if result.get('restored'):report['cp_cleanup']=result
            raise RuntimeError('CP operation failed: '+str(result))
        return result
    redirected=False;feature=False;link_raised=False
    try:
        report['controller']=response();save()
        link=json.loads(subprocess.check_output(['ip','-j','link','show','dev','enp8s0f1'],text=True))[0]
        report['capture_link_before']=link;save()
        if 'UP' not in link['flags']:
            link_raised=True
            subprocess.run(['ip','link','set','dev','enp8s0f1','up'],check=True)
        features=subprocess.check_output(['ethtool','-k','enp8s0f1'],text=True)
        if 'rx-all: off' not in features:raise RuntimeError('capture rx-all baseline must be off')
        subprocess.run(['ethtool','-K','enp8s0f1','rx-all','on'],check=True)
        feature=True;time.sleep(2)
        # Set rollback intent before the route mutation, including lost replies.
        redirected=True;route('session-path-enable')
        with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as sock:
            sock.bind(('enp8s0f1',0));sock.setblocking(False)
            index=socket.if_nametoindex('enp8s0f1')
            sock.setsockopt(263,1,struct.pack('IHH8s',index,1,0,bytes(8)))
            for phase,op in [('baseline','snapshot'),('miss','prepare'),('hit','install'),('drop','drop'),('removed','remove')]:
                before=command(op)['registers']
                report['phases'][phase]=capture_phase(sock,args.count)
                after=command('snapshot')['registers']
                p=report['phases'][phase]
                p['counter_delta']={k:v['raw']-before[k]['raw'] for k,v in after.items()
                                    if k.endswith('_no_rd_clr') and k in before}
                save()
                print(json.dumps({'phase':phase,'original':p['original'],'rewritten':p['rewritten'],
                                  'received':len(p['packets']),'report':str(path)}),flush=True)
                if phase=='baseline' and p['original']!=list(range(args.count)):
                    raise RuntimeError('physical baseline failed; parsed traffic not activated')
        report['session_offload_verified']=qualifies(report['phases'])
    except BaseException as e:
        report['error']=str(e);save()
        raise
    finally:
        if redirected:
            try:route('session-path-restore')
            except Exception as e:report['cleanup_errors'].append(str(e))
        try:
            if cp.poll() is None and not report.get('cp_cleanup',{}).get('restored'):
                cp.stdin.write('{"op":"finish"}\n');cp.stdin.flush()
                report['cp_cleanup']=response()
            cp.wait(timeout=30)
            if not report.get('cp_cleanup',{}).get('restored'):
                stderr.seek(0);raise RuntimeError('CP cleanup not verified: '+stderr.read()[-4000:])
        except Exception as e:report['cleanup_errors'].append(str(e))
        if feature:
            try:subprocess.run(['ethtool','-K','enp8s0f1','rx-all','off'],check=True)
            except Exception as e:report['cleanup_errors'].append(str(e))
        if link_raised:
            try:subprocess.run(['ip','link','set','dev','enp8s0f1','down'],check=True)
            except Exception as e:report['cleanup_errors'].append(str(e))
        report['session_offload_verified'] &= not bool(report['cleanup_errors'])
        save()
        print(json.dumps({'session_offload_verified':report['session_offload_verified'],
                          'cleanup_errors':report['cleanup_errors'],'report':str(path)}),flush=True)
    raise SystemExit(0 if report['session_offload_verified'] else 2)


if __name__=='__main__':main()
