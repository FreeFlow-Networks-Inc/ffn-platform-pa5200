#!/usr/bin/env python3
"""Pinned local OCTEON boot executor. Profiles are MP-provisioned root data."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import time


def sha(data): return hashlib.sha256(data).hexdigest()


def kernel_notes(data):
    if data[:6] != b'\x7fELF\x02\x02' or data[18:20] != b'\0\x08':
        raise ValueError('MIPS64 big-endian kernel required')
    offset = struct.unpack_from('>Q', data, 40)[0]
    size, count, names = struct.unpack_from('>HHH', data, 58)
    def section(i): return struct.unpack_from('>IIQQQQIIQQ', data, offset + i * size)
    strings = section(names); strings = data[strings[4]:strings[4] + strings[5]]
    for i in range(count):
        item = section(i)
        if strings[item[0]:].split(b'\0')[0] == b'.notes':
            return sha(data[item[4]:item[4] + item[5]])
    raise ValueError('Kernel notes missing')


def profile(role):
    if role not in ('cp', 'dp'): raise ValueError('Invalid processor')
    path = Path('/etc/ffn/plane-boot') / (role + '.json')
    stat = path.stat()
    if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022 or stat.st_size > 65536:
        raise ValueError('Protected root-owned boot profile required')
    cfg = json.loads(path.read_text())
    if cfg['schema'] != 1 or cfg['role'] != role: raise ValueError('Wrong boot profile')
    if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]',cfg['pci']):
        raise ValueError('Invalid PCI identity')
    if type(cfg['cores']) is not int or not 1 <= cfg['cores'] <= 64: raise ValueError('Invalid core count')
    if type(cfg['devnum']) is not int or not 0 <= cfg['devnum'] <= 7: raise ValueError('Invalid device ordinal')
    for field in ('extra', 'fdt'):
        if not isinstance(cfg[field],str) or not re.fullmatch(r'[A-Za-z0-9_.,=:/ -]*',cfg[field]):
            raise ValueError('Unsafe boot arguments')
    return cfg


def preflight(cfg):
    endpoint = Path('/sys/bus/pci/devices') / cfg['pci']
    identity = (endpoint/'vendor').read_text().strip()[2:] + (endpoint/'device').read_text().strip()[2:]
    expected = '177d9700' if cfg['role'] == 'cp' else '177d0095'
    if identity != expected: raise ValueError('Boot target is not the selected processor')
    ids = [line.split()[1] for line in Path('/proc/bus/pci/devices').read_text().splitlines()
           if line.split()[1].startswith('177d') and int(line.split()[0],16) & 7 == 0]
    if ids.count(expected) != 1 or ids.index(expected) != cfg['devnum']:
        raise ValueError('OCTEON enumeration changed; refusing a guessed reset target')
    for name, item in dict(cfg['tools'],kernel=cfg['kernel'],bootloader=cfg['bootloader']).items():
        path = Path(item['path'])
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 512*1024**2:
            raise ValueError('Missing or oversized pinned '+name)
        if sha(path.read_bytes()) != item['sha256']: raise ValueError('Pinned file changed: '+name)
    data = Path(cfg['kernel']['path']).read_bytes()
    if kernel_notes(data) != cfg['kernel']['notes_sha256']: raise ValueError('Kernel identity mismatch')
    versions = re.findall(rb'Linux version (\d+)\.(\d+)\.(\d+)',data)
    if not versions or any(tuple(map(int,v)) < (6,18,0) for v in versions):
        raise ValueError('Legacy kernels cannot be selected')
    if b'ffn_reserve' not in data: raise ValueError('Kernel lacks reserved transport memory')
    if cfg['role'] == 'dp' and len(command(cfg).encode()) > 247: raise ValueError('DP boot command exceeds mailbox')
    return {'validated':True,'role':cfg['role'],'kernel_sha256':cfg['kernel']['sha256'],
            'notes_sha256':cfg['kernel']['notes_sha256'],'pci':cfg['pci']}


def command(cfg):
    return 'bootoctlinux 21000000 numcores=%d console=ttyS0,115200n8%s rw%s' % (
        cfg['cores'], ' ffn_fdt='+cfg['fdt'] if cfg['fdt'] else '', ' '+cfg['extra'] if cfg['extra'] else '')


@contextmanager
def exclusive(path):
    with open(path,'a') as stream:
        deadline=time.monotonic()+30
        while True:
            try:fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise
                time.sleep(.1)
        try: yield
        finally: fcntl.flock(stream,fcntl.LOCK_UN)


def run(argv, env=None, timeout=180, hardware=False):
    # Never kill a vendor BAR transaction halfway through. On a deadline miss,
    # keep the owner/lock alive until the child exits, then fail without retry.
    proc = subprocess.Popen(argv,env=env)
    try: code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if not hardware:
            proc.terminate(); proc.wait(timeout=15)
        else:
            print('Hardware command deadline exceeded; retaining reset lock until it exits',flush=True)
            proc.wait()
        raise RuntimeError('Command deadline exceeded; no reset retry')
    if code: raise RuntimeError('Command failed: '+Path(argv[0]).name+' rc='+str(code))


def stop_transport(cfg):
    unit = cfg['transport']['unit']
    run(['systemctl','stop',unit],timeout=30)
    expected = cfg['transport']['argv']
    for p in Path('/proc').glob('[0-9]*'):
        try:
            argv = (p/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if argv == expected:
                os.kill(int(p.name),signal.SIGTERM)
        except (OSError,UnicodeError): pass
    time.sleep(1)
    # Drain bounded observations; refuse residual diagnostic/transport mappings.
    deadline=time.monotonic()+20
    while True:
        mapped=[]
        for p in Path('/proc').glob('[0-9]*'):
            try: maps=(p/'maps').read_text()
            except OSError: continue
            if cfg['pci']+'/resource' in maps:mapped.append(p.name)
        if not mapped:break
        if time.monotonic()>=deadline:raise RuntimeError('BAR still mapped by processes '+','.join(mapped)+'; reset refused')
        time.sleep(.2)


def boot(cfg):
    preflight(cfg)
    if cfg['role']=='dp':
        from plane_restart_node import observe
        previous=observe('dp')
    lock = '/run/ffn-octeon-ctl.lock' if cfg['role']=='cp' else '/run/ffn-dp-reset.lock'
    env=dict(os.environ,**cfg.get('environment',{}),FFN_OCTEON_LOCKED='1')
    def tool(name,*args):
        run([cfg['tools'][name]['path'],'--devnum='+str(cfg['devnum']),*args],env=env,hardware=True)
    enable=Path('/sys/bus/pci/devices')/cfg['pci']/'enable'
    def reenable():
        enable.write_text('0');time.sleep(1);enable.write_text('1')
    with exclusive(lock):
        reset=False
        try:
            stop_transport(cfg)
            reenable();reset=True
            tool('reset','nowait');time.sleep(2);reenable()
            tool('boot','--loadcache',cfg['bootloader']['path']);time.sleep(20);reenable()
            if cfg['role']=='cp':
                run(['/usr/bin/python3',cfg['tools']['stage']['path'],'--kernel',cfg['kernel']['path'],
                     '--cores',str(cfg['cores']),'--fdt',cfg['fdt'],'--extra',cfg['extra'],'--watch','90'],env=env,hardware=True)
                tool('csr','SPEM0_BAR1_INDEX1','0xa43')
            else:
                tool('load','0x21000000',cfg['kernel']['path']);reenable()
                # Only window zero (boot command) and one (recovery/transport) are required.
                for index,value in [(0,'0x1'),(1,'0x11')]: tool('csr','PEM0_BAR1_INDEX'+str(index),value)
                run(['/usr/bin/python3',cfg['tools']['stage']['path'],'--pci',cfg['pci'],'--wait','25',command(cfg)],env=env,hardware=True)
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    try:
                        now=observe('dp',owned=True)
                        if now['boot_id']!=previous['boot_id'] and now['notes_sha256']==cfg['kernel']['notes_sha256']:break
                    except (OSError,ValueError,RuntimeError):pass
                    time.sleep(3)
                else:raise RuntimeError('DP did not acknowledge a new boot; transport remains stopped')
            run(['systemctl','start',cfg['transport']['unit']],timeout=45)
            for _ in range(3):
                run(['systemctl','is-active','--quiet',cfg['transport']['unit']],timeout=10)
                time.sleep(1)
        except Exception:
            # After a reset, blindly restarting a BAR writer could hang the host.
            if not reset: run(['systemctl','start',cfg['transport']['unit']],timeout=45)
            raise
    return {'dispatched':True,'role':cfg['role'],'kernel_sha256':cfg['kernel']['sha256']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('role',choices=['cp','dp']);p.add_argument('action',choices=['preflight','boot'])
    a=p.parse_args()
    try: print(json.dumps((preflight if a.action=='preflight' else boot)(profile(a.role))))
    except Exception as e: p.exit(1,str(e)+'\n')
