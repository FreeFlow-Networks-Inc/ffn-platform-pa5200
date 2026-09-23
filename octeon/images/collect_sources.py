#!/usr/bin/env python3
"""Collect exact Debian source packages for an image inventory, including static runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packages'))
from runtime_inventory import paragraphs


def descriptor(path):
    text=path.read_text()
    # Clear-signed Debian control records have an armor header and footer.
    if text.startswith('-----BEGIN PGP SIGNED MESSAGE-----'):
        text=text.split('\n\n',1)[1].split('-----BEGIN PGP SIGNATURE-----',1)[0]
    return paragraphs(text.strip())[0]


def snapshot_source(name, version, directory):
    """Recover a superseded exact version from Debian's content-addressed archive."""
    base='https://snapshot.debian.org'
    def metadata(url):
        with urllib.request.urlopen(url,timeout=30) as response:
            data=response.read(1024*1024+1)
        if len(data)>1024*1024: raise ValueError('Oversized snapshot metadata')
        return json.loads(data)
    records=metadata(base+'/mr/package/'+urllib.parse.quote(name,safe='')+'/'+urllib.parse.quote(version,safe='')+'/srcfiles')
    if records.get('package')!=name or records.get('version')!=version:
        raise ValueError('Snapshot source identity mismatch')
    for item in records['result']:
        digest=item['hash']
        if not re.fullmatch('[0-9a-f]{40}',digest): raise ValueError('Invalid snapshot hash')
        info=metadata(base+'/mr/file/'+digest+'/info')['result'][0]
        filename,size=info['name'],info['size']
        if Path(filename).name!=filename or not 0<size<=3*1024**3:
            raise ValueError('Unsafe snapshot file')
        target=directory/filename
        temp=target.with_name(target.name+'.partial')
        count=0;hasher=hashlib.sha1()
        try:
            with urllib.request.urlopen(base+'/file/'+digest,timeout=60) as response,temp.open('wb') as output:
                while block:=response.read(1024*1024):
                    count+=len(block)
                    if count>size: raise ValueError('Snapshot size exceeded')
                    output.write(block);hasher.update(block)
            if count!=size or hasher.hexdigest()!=digest: raise ValueError('Snapshot content address mismatch')
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    # The caller additionally checks every archive against the .dsc SHA-256.


def collect(root, caches, out):
    required=set()
    for package in paragraphs((root/'var/lib/dpkg/status').read_text()):
        if package.get('Status')!='install ok installed':
            raise ValueError('Unconfigured package in source inventory')
        source=package.get('Source',package['Package'])
        match=re.fullmatch(r'([a-z0-9+.-]+)(?: \(([^)]+)\))?',source)
        if not match: raise ValueError('Invalid source package identity')
        required.add((match[1],match[2] or re.sub(r'\+b[0-9]+$','',package['Version'])))
        for name,version in re.findall(r'([a-z0-9+.-]+) \(= ([^)]+)\)',package.get('Built-Using','')):
            required.add((name,version))
    # busybox-static is used by the initramfs, while the runtime installs busybox.
    # They are produced from the same source version already in this inventory.
    out.mkdir(parents=True,exist_ok=True)
    index={}
    for cache in [out,*caches]:
        for path in cache.rglob('*.dsc'):
            meta=descriptor(path)
            index.setdefault((meta['Source'],meta['Version']),path)
    records=[]
    for name,version in sorted(required):
        if not re.fullmatch(r'[A-Za-z0-9.+:~_-]+',version): raise ValueError('Invalid source version')
        directory=out/name;directory.mkdir(exist_ok=True)
        path=index.get((name,version))
        if path is None:
            result=subprocess.run(['apt-get','source','--download-only',name+'='+version],cwd=directory)
            if result.returncode:
                snapshot_source(name,version,directory)
            matches=[p for p in directory.glob('*.dsc') if descriptor(p).get('Version')==version]
            if len(matches)!=1: raise ValueError('Expected one exact source descriptor for '+name)
            path=matches[0]
        meta=descriptor(path)
        artifacts={}
        for line in meta['Checksums-Sha256'].splitlines():
            if not line.strip(): continue
            digest,size,filename=line.split()
            if Path(filename).name!=filename or not re.fullmatch('[0-9a-f]{64}',digest):
                raise ValueError('Unsafe source descriptor')
            source=path.parent/filename
            with source.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=digest or source.stat().st_size!=int(size): raise ValueError('Source checksum mismatch: '+str(source))
            target=directory/filename
            if source.resolve()!=target.resolve(): shutil.copyfile(source,target)
            artifacts[filename]=digest
        target=directory/path.name
        if path.resolve()!=target.resolve(): shutil.copyfile(path,target)
        artifacts[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        records.append({'source':name,'version':version,'artifacts':artifacts})
        print('SOURCE',name,version,flush=True)
    report={'schema':1,'sources':records,'package_count':len(records)}
    (out/'sources.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rootfs',type=Path,required=True)
    p.add_argument('--cache',type=Path,action='append',default=[])
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();collect(a.rootfs,a.cache,a.out)
