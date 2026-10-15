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
    transport_service(cfg)
    if cfg.get('root_server_unit'):
        transport_service({'transport':{'unit':cfg['root_server_unit']}})
    endpoint = Path('/sys/bus/pci/devices') / cfg['pci']
    identity = (endpoint/'vendor').read_text().strip()[2:] + (endpoint/'device').read_text().strip()[2:]
    expected = '177d9700' if cfg['role'] == 'cp' else '177d0095'
    if identity != expected: raise ValueError('Boot target is not the selected processor')
    ids = [line.split()[1] for line in Path('/proc/bus/pci/devices').read_text().splitlines()
           if line.split()[1].startswith('177d') and int(line.split()[0],16) & 7 == 0]
    if ids.count(expected) != 1 or ids.index(expected) != cfg['devnum']:
        raise ValueError('OCTEON enumeration changed; refusing a guessed reset target')
    pinned = dict(cfg['tools'],kernel=cfg['kernel'],bootloader=cfg['bootloader'])
    for name in ('rootfs','provision'):
        if cfg.get(name):
            if cfg['role'] != 'dp': raise ValueError('A staged RAM root is a DP profile entry')
            pinned[name] = cfg[name]
    for name, item in pinned.items():
        path = Path(item['path'])
        limit = 1024**3 if name == 'rootfs' else 512*1024**2
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError('Missing or oversized pinned '+name)
        if sha(path.read_bytes()) != item['sha256']: raise ValueError('Pinned file changed: '+name)
    if cfg.get('provision') and not cfg.get('rootfs'): raise ValueError('Provisioning requires a staged RAM root')
    rootfs_plan(cfg)   # layout and image format; a bad entry fails here, not mid-reset
    data = Path(cfg['kernel']['path']).read_bytes()
    if kernel_notes(data) != cfg['kernel']['notes_sha256']: raise ValueError('Kernel identity mismatch')
    versions = re.findall(rb'Linux version (\d+)\.(\d+)\.(\d+)',data)
    if not versions or any(tuple(map(int,v)) < (6,18,0) for v in versions):
        raise ValueError('Legacy kernels cannot be selected')
    if b'ffn_reserve' not in data: raise ValueError('Kernel lacks reserved transport memory')
    if cfg['role'] == 'dp' and len(command(cfg).encode()) > 247: raise ValueError('DP boot command exceeds mailbox')
    return {'validated':True,'role':cfg['role'],'kernel_sha256':cfg['kernel']['sha256'],
            'notes_sha256':cfg['kernel']['notes_sha256'],'pci':cfg['pci']}


# DP DRAM inside u-boot's own load area. Measured on the PA-5220 (2026-10-15,
# bootmem walk with the DP halted in u-boot): named block __tmp_load covers
# 0x20000000-0x30000000 (the kernel is staged at 0x21000000 inside it),
# __tmp_reserved_linux covers 0x100000-0xf100000, and the first big free block
# starts at 0x30000000 with its list header stored in place. A root staged at
# 0x28000000 ran past 0x30000000, overwrote that header, and bootoctlinux could
# not allocate the kernel's segments any more: command consumed, no kernel.
ROOTFS_ADDR = 0x24000000     # 64 MiB above __tmp_load's start, clear of the staged kernel
ROOTFS_END = 0x30000000      # __tmp_load's end: the first free-block header lives here
ROOTFS_HEADER = 4096         # the staged header block
MIB = 1 << 20


def mib_up(size): return (size + MIB - 1) // MIB


def rootfs_plan(cfg):
    """Where the DP's RAM root goes in its DRAM, and the boot arguments that
    describe it. None for a profile with no `rootfs` entry (NFS root).

    Layout, all 1 MiB aligned so one ffn_reserve covers it and one phram
    device exposes it: header at +0, the squashfs at +1 MiB, an optional
    provisioning tar after the image rounded up to the next MiB. The header
    is a text line with the sizes and SHA-256 of both payloads; the DP's
    initramfs refuses to mount anything that does not match it, which is what
    turns a bad PCIe transfer into a recovery console rather than a boot.
    """
    if not cfg.get('rootfs'): return None
    item = cfg['rootfs']
    name = item.get('name', 'dproot')
    if not re.fullmatch(r'[a-z][a-z0-9]{0,15}', name): raise ValueError('Invalid RAM root device name')
    image = Path(item['path'])
    if not image.is_absolute() or not image.is_file(): raise ValueError('Missing pinned rootfs image')
    with image.open('rb') as stream:
        if stream.read(4) != b'hsqs': raise ValueError('Pinned rootfs is not a squashfs image')
    if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']): raise ValueError('Rootfs digest required')
    size = image.stat().st_size
    if not 0 < size <= 1024**3: raise ValueError('Rootfs image size out of range')
    plan = {'name': name, 'address': ROOTFS_ADDR, 'image': image, 'image_size': size,
            'image_sha256': item['sha256'], 'image_offset': MIB,
            'provision': None, 'provision_size': 0, 'provision_sha256': '',
            'provision_offset': MIB + mib_up(size) * MIB}
    end = plan['provision_offset']
    if cfg.get('provision'):
        extra = cfg['provision']
        archive = Path(extra['path'])
        if not archive.is_absolute() or not archive.is_file(): raise ValueError('Missing pinned provisioning archive')
        with archive.open('rb') as stream:
            stream.seek(257)
            if stream.read(5) != b'ustar': raise ValueError('Provisioning archive must be a plain tar')
        if not re.fullmatch(r'[0-9a-f]{64}', extra['sha256']): raise ValueError('Provisioning digest required')
        psize = archive.stat().st_size
        if not 0 < psize <= 64 * MIB: raise ValueError('Provisioning archive size out of range')
        plan.update(provision=archive, provision_size=psize, provision_sha256=extra['sha256'])
        end += mib_up(psize) * MIB
    plan['reserve_mib'] = end // MIB
    if ROOTFS_ADDR + end > ROOTFS_END: raise ValueError('RAM root does not fit under u-boot load area end 0x%x' % ROOTFS_END)
    plan['header'] = ('FFN-ROOTFS 1 %d %s%s\n' % (size, item['sha256'],
                      ' %d %s' % (plan['provision_size'], plan['provision_sha256']) if plan['provision'] else '')
                      ).encode().ljust(ROOTFS_HEADER, b'\0')
    plan['arguments'] = 'ffn_reserve=0x%x,%dM phram.phram=%s,0x%x,%dMi ffn.rootfs=%s' % (
        ROOTFS_ADDR, plan['reserve_mib'], name, ROOTFS_ADDR, plan['reserve_mib'], name)
    return plan


def command(cfg):
    plan = rootfs_plan(cfg) if cfg.get('role') == 'dp' else None
    return 'bootoctlinux 21000000 numcores=%d console=ttyS0,115200n8%s rw%s%s' % (
        cfg['cores'], ' ffn_fdt='+cfg['fdt'] if cfg['fdt'] else '', ' '+cfg['extra'] if cfg['extra'] else '',
        ' '+plan['arguments'] if plan else '')


def transport_service(cfg):
    unit=cfg['transport']['unit']
    p=subprocess.run(['systemctl','show',unit,'--property=LoadState,FragmentPath,Transient'],
                     capture_output=True,text=True,check=True,timeout=10)
    state=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
    fragment=state.get('FragmentPath','')
    if (state.get('LoadState')!='loaded' or state.get('Transient')!='no'
            or not fragment.startswith(('/etc/systemd/system/','/usr/lib/systemd/system/','/lib/systemd/system/'))
            or not Path(fragment).is_file()):
        raise ValueError('Persistent transport service required before reset: '+unit)
    return state


def start_root_server(cfg):
    if cfg.get('root_server_unit'):
        run(['systemctl','start',cfg['root_server_unit']],timeout=90)
        run(['systemctl','is-active','--quiet',cfg['root_server_unit']],timeout=10)


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
    # A former transient unit can retain its /run fragment until stopped, even
    # when a persistent replacement was installed. Reload and validate before
    # touching the processor, rather than discovering a missing unit after boot.
    run(['systemctl','daemon-reload'],timeout=30)
    transport_service(cfg)
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


def stage_rootfs(cfg,tool,reenable):
    """Load the RAM root into DP DRAM. Must run while u-boot is still up and
    before the kernel is loaded: the loader's final step talks to the
    bootloader mailbox, so staging after Linux starts reports failure even
    though the bytes landed, and the kernel's own staging area is separate."""
    plan=rootfs_plan(cfg)
    if plan is None:return None
    header=Path('/run/ffn-dp-rootfs-header')
    header.write_bytes(plan['header']);header.chmod(0o600)
    for offset,path in ((0,header),(plan['image_offset'],plan['image']),(plan['provision_offset'],plan['provision'])):
        if path is None:continue
        tool('load','0x%x'%(plan['address']+offset),str(path));reenable()
    return plan


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
            # The DP's initramfs mounts its NFS root as soon as the CP answers a
            # ping, so the export must exist before the transport comes back.
            if cfg['role']=='dp': start_root_server(cfg)
            stop_transport(cfg)
            reenable();reset=True
            tool('reset','nowait');time.sleep(2);reenable()
            tool('boot','--loadcache',cfg['bootloader']['path']);time.sleep(20);reenable()
            if cfg['role']=='cp' and cfg.get('fpga_step'):
                # Any OCTEON reset leaves the CA1/CE40 socket on the a101 image; only
                # u-boot's fpga_program can load ce40.bin, so it happens here, in the
                # session that goes on to boot Linux. Never fatal: the step exits 0 and
                # records the personality in /run/ffn-ce40-personality.
                run(['/bin/bash',cfg['fpga_step']],env=env,timeout=960,hardware=True)
            if cfg['role']=='cp':
                run(['/usr/bin/python3',cfg['tools']['stage']['path'],'--kernel',cfg['kernel']['path'],
                     '--cores',str(cfg['cores']),'--fdt',cfg['fdt'],'--extra',cfg['extra'],'--watch','15'],env=env,hardware=True)
                tool('csr','SPEM0_BAR1_INDEX1','0xa43')
            else:
                stage_rootfs(cfg,tool,reenable)
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
            start_root_server(cfg)
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
