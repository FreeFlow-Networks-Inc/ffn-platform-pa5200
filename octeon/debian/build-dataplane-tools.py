#!/usr/bin/env python3
"""Build the DP userspace toolkit inside the Debian sid build host.

Run as root inside sid-host. Uses authenticated APT source acquisition and the
mips64-linux-gnuabi64 cross compiler. Outputs a private prefix, not a rootfs
overwrite. No target services are started by this build.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess as S
import tarfile
import time

HOST='mips64-linux-gnuabi64'
PREFIX='/usr/local/ffn-dp'
PACKAGES=['libmnl','libnftnl','jansson','nftables','libnfnetlink','libnetfilter-conntrack',
          'libtirpc','libnetfilter-cthelper','libnetfilter-cttimeout','libnetfilter-queue',
          'conntrack-tools','libpcap','tcpdump','ethtool','iperf3','traceroute','iputils','socat']


def run(argv,cwd,env,log):
    with log.open('a') as stream:
        stream.write('\n$ '+repr(argv)+'\n');stream.flush()
        S.run(argv,cwd=cwd,env=env,stdout=stream,stderr=S.STDOUT,check=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,default=Path('/build/ffn-dp-tools'))
    p.add_argument('--package-only',action='store_true');p.add_argument('packages',nargs='*',choices=PACKAGES);a=p.parse_args()
    work=a.work.resolve();work.mkdir(parents=True,exist_ok=True);prefix=Path(PREFIX);prefix.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,CC=HOST+'-gcc',AR=HOST+'-ar',RANLIB=HOST+'-ranlib',STRIP=HOST+'-strip',
             PKG_CONFIG_LIBDIR=PREFIX+'/lib/pkgconfig',PKG_CONFIG_PATH='',
             CPPFLAGS='-I'+PREFIX+'/include',LDFLAGS='-L'+PREFIX+'/lib -Wl,-rpath,'+PREFIX+'/lib',
             CFLAGS='-O2 -mabi=64 -march=mips64r2',DEBIAN_FRONTEND='noninteractive')
    manifest_path=work/'manifest.json';manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {'target':HOST,'prefix':PREFIX,'packages':{}}
    for package in ([] if a.package_only else a.packages or PACKAGES):
        directory=work/package;directory.mkdir(exist_ok=True);log=directory/'build.log'
        print('Building '+package,flush=True)
        try:
            if not list(directory.glob('*.dsc')):
                run(['apt-get','source','--download-only',package],directory,env,log)
            dsc=next(directory.glob('*.dsc'));source=directory/'source'
            if not source.exists():run(['dpkg-source','-x',str(dsc),str(source)],directory,env,log)
            if package=='traceroute':
                run(['make','-j4','CC='+HOST+'-gcc','prefix='+PREFIX],source,env,log)
                run(['make','install','prefix='+PREFIX],source,env,log)
            elif package=='iputils':
                cross=directory/'cross.ini'
                cross.write_text("[binaries]\nc = '"+HOST+"-gcc'\nar = '"+HOST+"-ar'\nstrip = '"+HOST+"-strip'\npkg-config = 'pkg-config'\n[host_machine]\nsystem = 'linux'\ncpu_family = 'mips64'\ncpu = 'mips64r2'\nendian = 'big'\n")
                run(['meson','setup','out','--cross-file',str(cross),'--prefix',PREFIX,'-DUSE_CAP=false','-DUSE_IDN=false','-DBUILD_MANS=false','-DBUILD_HTML_MANS=false','-DSKIP_TESTS=true','-DUSE_GETTEXT=false'],source,env,log)
                run(['ninja','-C','out','install'],source,env,log)
            else:
                if not (source/'configure').exists() or package in ('libnetfilter-conntrack','libtirpc'):run(['autoreconf','-fi'],source,env,log)
                options={
                    'nftables':['--disable-python','--disable-man-doc','--without-cli','--with-mini-gmp','--with-json'],
                    'libtirpc':['--disable-gssapi'],
                    'conntrack-tools':['--disable-systemd'],
                    'libpcap':['--disable-dbus','--without-libnl','--disable-rdma','--disable-usb','--disable-bluetooth'],
                    'tcpdump':['--without-crypto','--disable-smb'],
                    'iperf3':['--without-openssl','--without-sctp'],
                    'socat':['--disable-openssl','--disable-libwrap','--disable-readline'],
                }.get(package,[])
                run(['./configure','--host='+HOST,'--build=x86_64-linux-gnu','--prefix='+PREFIX,'--libdir='+PREFIX+'/lib','--disable-static',*options],source,env,log)
                run(['make','-j4'],source,env,log);run(['make','install'],source,env,log)
            manifest['packages'][package]={'status':'built','dsc':dsc.name,'sources':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in directory.iterdir() if f.is_file() and (f.suffix=='.dsc' or '.tar.' in f.name)}}
            print(package+' built',flush=True)
        except Exception as error:
            manifest['packages'][package]={'status':'failed','error':str(error),'log':str(log)}
            print(package+' failed: '+str(log),flush=True)
        manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    for package in manifest['packages']:
        source=work/package/'source';licenses=prefix/'share/licenses'/package;licenses.mkdir(parents=True,exist_ok=True)
        for name in ('debian/copyright','COPYING','COPYING.LIB','LICENSE'):
            if (source/name).is_file():shutil.copy2(source/name,licenses/Path(name).name)
    files={}
    for f in prefix.rglob('*'):
        if not f.is_file() or f.is_symlink():continue
        data=f.read_bytes()
        if data[:4]==b'\x7fELF':
            if data[4:6]!=b'\x02\x02' or int.from_bytes(data[18:20],'big')!=8:raise SystemExit('Wrong target ABI: '+str(f))
            files[str(f.relative_to(prefix))]=hashlib.sha256(data).hexdigest()
    manifest['elf_files']=files;manifest['built_at']=int(time.time());manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    (prefix/'manifest.json').write_text(manifest_path.read_text())
    with tarfile.open(work/'ffn-dp-tools-mips64eb.tar.gz','w:gz') as archive:
        archive.add(prefix,arcname=PREFIX.lstrip('/'))
    if any(v['status']!='built' for v in manifest['packages'].values()):raise SystemExit(1)


if __name__=='__main__':main()
