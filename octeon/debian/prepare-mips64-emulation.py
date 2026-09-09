#!/usr/bin/env python3
"""Register only ELF64 big-endian MIPS for this VM's static QEMU interpreter."""
from pathlib import Path
import subprocess

base = Path('/mnt/clones/debian-mips64/sid-host')
interpreter = base / 'usr/bin/qemu-mips64'
assert interpreter.is_file(), interpreter
registry = Path('/proc/sys/fs/binfmt_misc')
assert (registry / 'register').exists(), 'binfmt_misc must be mounted'
name = 'ffn-mips64-be'
magic = b'\x7fELF\x02\x02\x01' + bytes(9) + b'\x00\x02\x00\x08'
mask = bytes([255])*7 + bytes(9) + b'\xff\xfe\xff\xff'
escape = lambda data: ''.join('\\x%02x' % byte for byte in data)
registration = f':{name}:M::{escape(magic)}:{escape(mask)}:{interpreter}:F'
if not (registry / name).exists():
    (registry / 'register').write_text(registration)
print((registry / name).read_text())
# The cross-installed libc provides the real interpreter in the multiarch dir.
loader = base / 'usr/lib64/ld.so.1'
if not loader.exists():
    loader.symlink_to('../lib/mips64-linux-gnuabi64/ld.so.1')
subprocess.run(['chroot', str(base), '/usr/lib/mips64-linux-gnuabi64/ld.so.1', '--version'], check=True)
# arch-test ships no mips64 helper. Compile a real static target executable
# so mmdebstrap can verify execution instead of skipping its architecture check.
probe = base / 'tmp/ffn-arch-test.c'
probe.write_text('#include <stdio.h>\nint main(void) { puts("ok"); return 0; }\n')
subprocess.run(['chroot', str(base), 'mips64-linux-gnuabi64-gcc', '-static',
                '/tmp/ffn-arch-test.c', '-o', '/usr/libexec/arch-test/mips64'], check=True)
subprocess.run(['chroot', str(base), 'arch-test', 'mips64'], check=True)
