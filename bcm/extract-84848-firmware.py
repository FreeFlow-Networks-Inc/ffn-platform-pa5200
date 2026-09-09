#!/usr/bin/env python3
"""MP: extract owner-supplied BCM84848 RAM firmware from its existing SDK ELF.

Never put the output in the repository or an image; retain it with the
appliance's owner-supplied runtime files.
"""
import argparse
import hashlib
import pathlib
import struct
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('elf')
p.add_argument('output')
a = p.parse_args()
symbols = {}
for line in subprocess.check_output(['readelf', '-Ws', a.elf], text=True).splitlines():
    fields = line.split()
    if len(fields) == 8 and fields[7] in ('bcm_84844_firmware', 'bcm_84844_firmware_size'):
        symbols[fields[7]] = (int(fields[1], 16), int(fields[2], 16 if fields[2].startswith('0x') else 10))
if len(symbols) != 2:
    raise SystemExit('required firmware symbols missing')
with open(a.elf, 'rb') as f:
    header = f.read(64)
    if header[:6] != b'\x7fELF\x02\x02' or struct.unpack_from('>H', header, 18)[0] != 8:
        raise SystemExit('expected a MIPS64 big-endian ELF')
    phoff = struct.unpack_from('>Q', header, 32)[0]
    phsize, phnum = struct.unpack_from('>HH', header, 54)
    segments = []
    for i in range(phnum):
        f.seek(phoff + i * phsize)
        segments.append(struct.unpack('>IIQQQQQQ', f.read(56)))
    def read_va(addr, size):
        for typ, flags, off, va, pa, filesz, memsz, align in segments:
            if typ == 1 and va <= addr and addr + size <= va + filesz:
                f.seek(off + addr - va)
                data = f.read(size)
                if len(data) != size: raise ValueError('truncated ELF')
                return data
        raise ValueError('firmware symbol outside file-backed load segments')
    size = struct.unpack('>I', read_va(symbols['bcm_84844_firmware_size'][0], 4))[0]
    address, capacity = symbols['bcm_84844_firmware']
    if not 0 < size <= capacity or size % 4:
        raise SystemExit('invalid firmware size')
    firmware = read_va(address, size)
out = pathlib.Path(a.output)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(firmware)
print('%d bytes SHA256 %s' % (size, hashlib.sha256(firmware).hexdigest()))
