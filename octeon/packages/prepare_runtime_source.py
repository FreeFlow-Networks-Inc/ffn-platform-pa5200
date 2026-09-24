#!/usr/bin/env python3
"""Versioned Debian packaging adjustments for the FFN MIPS64 runtime build."""
from email.utils import formatdate
from pathlib import Path
import argparse
import os


def replace(path, old, new):
    text = path.read_text()
    if old not in text:
        raise ValueError('Source layout changed: ' + str(path))
    path.write_text(text.replace(old, new))


def prepare(name, root):
    root = Path(root)
    changelog = root / 'debian/changelog'
    text = changelog.read_text()
    if '+ffn1)' in text.splitlines()[0]:
        return
    if name == 'openssh':
        replace(root/'debian/rules', 'ifeq ($(DEB_HOST_ARCH),ppc64el)',
                'ifneq (,$(filter ppc64el mips64,$(DEB_HOST_ARCH)))')
        reason = 'Disable unsupported zero-call-used-regs on MIPS64 (GCC RTL ICE); retain other hardening.'
    elif name == 'python3.14':
        for filename in ('control', 'control.in'):
            path = root/'debian'/filename
            data = path.read_text()
            # Only build dependencies change. Runtime dependencies stay intact.
            head, rest = data.split('\nPackage:', 1)
            head = head.replace('  systemtap-sdt-dev [!hurd-amd64 !hurd-i386],\n', '')
            head = head.replace('  valgrind-if-available,\n', '')
            path.write_text(head + '\nPackage:' + rest)
        replace(root/'debian/rules', '$(if $(or $(filter hurd,$(DEB_HOST_ARCH_OS)),$(filter hppa,$(DEB_HOST_ARCH))),,--with-dtrace)',
                '$(if $(or $(filter hurd,$(DEB_HOST_ARCH_OS)),$(filter hppa mips64,$(DEB_HOST_ARCH))),,--with-dtrace)')
        # The image runtime does not carry external tracing or valgrind agents.
        replace(root/'debian/rules', 'ifneq (,$(filter valgrind, $(shell dpkg-query --show -f \'$${Depends}\\n\' valgrind-if-available)))',
                'ifneq ($(DEB_HOST_ARCH),mips64)\nifneq (,$(filter valgrind, $(shell dpkg-query --show -f \'$${Depends}\\n\' valgrind-if-available)))')
        replace(root/'debian/rules', '  valgrind_configure_args = --with-valgrind\nendif',
                '  valgrind_configure_args = --with-valgrind\nendif\nendif')
        reason = 'Build the MIPS64 runtime without external DTrace/Valgrind instrumentation; use native build utilities.'
    elif name == 'nfs-utils':
        replace(root/'debian/control', ' libldap2-dev,', '')
        replace(root/'debian/control', ' libdevmapper-dev,', '')
        replace(root/'debian/rules', '--enable-junction \\', '--enable-junction --disable-ldap --disable-blkmapd \\')
        reason = 'Build native internal NFS with NSS/GSS mapping; omit LDAP idmapper and pNFS block-layout daemon.'
    elif name == 'nftables':
        # Its Python binding uses ctypes, not a target Python C extension.
        # Native Python packaging avoids incompatible cross-architecture
        # libpython versions while the nft/libnftables ELF remains MIPS64.
        path = root/'debian/control'
        data = path.read_text()
        head, rest = data.split('\nPackage:', 1)
        for dependency in ('libpython3-all-dev',):
            head = head.replace(dependency + ',', dependency + ':native,')
            head = head.replace(dependency + '\n', dependency + ':native\n')
        path.write_text(head + '\nPackage:' + rest)
        replace(root/'debian/rules', 'export PYBUILD_NAME = $(DEB_SOURCE)',
                'export PYBUILD_NAME = $(DEB_SOURCE)\n'
                '# The ctypes wheel is pure Python; only nft/libnftables are cross compiled.\n'
                'export _PYTHON_SYSCONFIGDATA_NAME := $(shell python3 -c "import sysconfig; print(sysconfig._get_sysconfigdata_name())")')
        reason = 'Use native Python tools for the pure ctypes binding during MIPS64 cross builds.'
    else:
        raise ValueError('Unsupported runtime source')
    version = text.split('(', 1)[1].split(')', 1)[0]
    date = formatdate(int(os.environ.get('SOURCE_DATE_EPOCH', '1790121600')), localtime=False).replace(' GMT', ' +0000')
    changelog.write_text(name + ' (' + version + '+ffn1) unstable; urgency=medium\n\n  * ' + reason +
                        '\n\n -- FFN Builder <builder@localhost.invalid>  ' + date + '\n\n' + text)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('package', choices=('openssh','python3.14','nfs-utils','nftables'))
    p.add_argument('source', type=Path)
    args = p.parse_args(); prepare(args.package, args.source)
