#!/usr/bin/env python3
"""Check target ELF headers and run native userspace/compiler smoke tests."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
assert root != Path('/') and (root / 'etc/debian_version').is_file()
executables = ['usr/bin/dpkg', 'usr/bin/bash', 'usr/bin/perl', 'usr/lib/systemd/systemd']
for name in executables:
    path = root / name
    header = path.read_bytes()[:20]
    assert header[:7] == b'\x7fELF\x02\x02\x01', (name, header)
    assert header[18:20] == b'\x00\x08', (name, header)
    print('ELF64 big-endian MIPS:', name)

env = dict(os.environ, LC_ALL='C', LANG='C')
def run(*args):
    result = subprocess.run(['chroot', str(root), *args], env=env,
                            text=True, capture_output=True)
    if result.returncode:
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        result.check_returncode()
    return result.stdout

assert run('dpkg', '--print-architecture').strip() == 'mips64'
architectures = set(run('dpkg-query', '-W', '-f=${Architecture}\n').splitlines())
assert architectures <= {'mips64', 'all'}, architectures
audit = run('dpkg', '--audit').strip()
assert not audit, 'unconfigured packages: ' + audit
print(run('perl', '-e', 'print "Perl target runtime OK\\n";'), end='')
print(run('/usr/lib/systemd/systemd', '--version').splitlines()[0])
repo = Path('/mnt/clones/debian-mips64/sid-host/tmp/repo')
mountpoint = root / 'tmp/repo'
mountpoint.mkdir(parents=True, exist_ok=True)
subprocess.run(['mount', '--bind', str(repo), str(mountpoint)], check=True)
try:
    subprocess.run(['mount', '-o', 'remount,bind,ro', str(mountpoint)], check=True)
    print(run('apt-get', '-o', 'Acquire::Languages=none', '-o',
              'APT::Update::Error-Mode=any', 'update'), end='')
finally:
    subprocess.run(['umount', str(mountpoint)], check=True)
if (root / 'usr/bin/gcc').exists():
    smoke = root / 'root/ffn-smoke'
    smoke.mkdir(parents=True, exist_ok=True)
    (smoke / 'hello.c').write_text('#include <stdio.h>\nint main(void) { unsigned int x=1; if (sizeof(void*)!=8 || *(unsigned char*)&x!=0) return 1; puts("C: MIPS64 BE n64 OK"); return 0; }\n')
    (smoke / 'hello.cc').write_text('#include <iostream>\n#include <vector>\nint main() { std::vector<int> v{1,2,3}; if(v.size()!=3) return 1; std::cout << "C++ target runtime OK\\n"; }\n')
    run('gcc', '/root/ffn-smoke/hello.c', '-o', '/root/ffn-smoke/hello-c')
    run('g++', '/root/ffn-smoke/hello.cc', '-o', '/root/ffn-smoke/hello-cxx')
    print(run('/root/ffn-smoke/hello-c'), end='')
    print(run('/root/ffn-smoke/hello-cxx'), end='')
    package = smoke / 'package'
    (package / 'debian').mkdir(parents=True, exist_ok=True)
    (package / 'hello.c').write_text((smoke / 'hello.c').read_text())
    (package / 'Makefile').write_text('all: ffn-toolchain-smoke\nffn-toolchain-smoke: hello.c\n\t$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $<\ninstall: all\n\tinstall -D -m755 ffn-toolchain-smoke $(DESTDIR)/usr/bin/ffn-toolchain-smoke\nclean:\n\trm -f ffn-toolchain-smoke\n')
    (package / 'debian/control').write_text('Source: ffn-toolchain-smoke\nSection: devel\nPriority: optional\nMaintainer: FFN Builder <builder@localhost.invalid>\nBuild-Depends: debhelper-compat (= 13)\nStandards-Version: 4.7.2\nRules-Requires-Root: no\n\nPackage: ffn-toolchain-smoke\nArchitecture: any\nDepends: ${shlibs:Depends}, ${misc:Depends}\nDescription: MIPS64 big-endian toolchain smoke test\n Verifies native compilation and Debian binary package generation.\n')
    (package / 'debian/changelog').write_text('ffn-toolchain-smoke (1.0) unstable; urgency=medium\n\n  * Validate the MIPS64 big-endian native build environment.\n\n -- FFN Builder <builder@localhost.invalid>  Tue, 08 Sep 2026 00:00:00 +0000\n')
    rules = package / 'debian/rules'
    rules.write_text('#!/usr/bin/make -f\n%:\n\tdh $@\n')
    rules.chmod(0o755)
    (package / 'debian/copyright').write_text('This smoke test was created for this build environment.\nLicense: CC0-1.0\n')
    print(run('/bin/sh', '-c', 'cd /root/ffn-smoke/package && dpkg-buildpackage -us -uc -b'), end='')
    result_deb = '/root/ffn-smoke/ffn-toolchain-smoke_1.0_mips64.deb'
    assert run('dpkg-deb', '-f', result_deb, 'Architecture').strip() == 'mips64'
    print('Native Debian .deb build passed:', result_deb)
print('Root filesystem checks passed')
