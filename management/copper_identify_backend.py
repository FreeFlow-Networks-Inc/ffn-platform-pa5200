#!/usr/bin/env python3
"""MP resource adapter for the fixed CP copper identification controller."""
import json
import shlex
import subprocess
import sys


def cp(action,payload):
    if action not in ('status','validate','apply'):raise ValueError('invalid action')
    result=subprocess.run(['ssh','-F','/etc/ffn-ngfw/ssh-cp.conf','-o','BatchMode=yes',
        '-o','ConnectTimeout=5','ffn-cp','python3 /usr/local/sbin/ffn_copper_identify.py '+action],
        input=json.dumps(payload),text=True,capture_output=True,timeout=15)
    data=json.loads(result.stdout)
    if result.returncode:raise ValueError(data.get('error','CP identification failed'))
    return data


PROFILE_PROGRAM = r'''
import json,sys,subprocess
from pathlib import Path
sys.path.insert(0,'/usr/local/sbin')
from ffn_copper_vif import validate_profile
from ffn_vif_runtime import atomic
import fcntl
request=json.load(sys.stdin)
wanted={'version':1,'ports':{p:dict(m,packet_path_verified=False) for p,m in request['mapping'].items()}}
validate_profile(wanted)
with open('/run/ffn-vif.lock','a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if json.loads(Path('/etc/ffn/vifs.json').read_text())['vifs']:
  raise ValueError('remove VIF assignments before correcting physical labels')
 service=subprocess.run(['systemctl','is-active','ffn-vif.service'],text=True,capture_output=True)
 if service.returncode!=3 or service.stdout.strip()!='inactive':
  raise ValueError('stop VIF transport before correcting physical labels')
 path=Path('/etc/ffn/vif-copper.json');old=json.loads(path.read_text());validate_profile(old)
 expected={'version':1,'ports':{'2':{'phy':17,'bcm_port':28,'packet_path_verified':False}}}
 if old not in (expected,wanted):raise ValueError('DP copper profile has other assignments; explicit review required')
 if request['apply'] and old!=wanted:
  atomic(path.with_name('vif-copper.previous.json'),old);atomic(path,wanted)
 print(json.dumps({'validated':True,'applied':request['apply']}))
'''


def dp_profile(mapping,write):
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',
        '-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts',
        '-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp',
        'root@127.1.2.2','python3 -c '+shlex.quote(PROFILE_PROGRAM)],
        input=json.dumps({'mapping':mapping,'apply':write}),text=True,capture_output=True,timeout=15)
    if result.returncode:raise ValueError('DP profile correction blocked; verify stopped, empty VIFs and the previous mapping')
    return json.loads(result.stdout)


def execute(action,payload):
    if action not in ('status','validate','apply'):raise ValueError('invalid action')
    if payload.get('operation')=='correct-wan-label':
        planned=cp('validate',payload)
        dp_profile(planned['mapping'],action=='apply')
        if action=='validate':return planned
    return cp(action,payload)


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise ValueError('request too large')
        print(json.dumps(execute(sys.argv[1],json.loads(raw))))
    except Exception as e:print(json.dumps({'error':str(e)[:512]}));sys.exit(2)
