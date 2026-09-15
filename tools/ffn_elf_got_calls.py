#!/usr/bin/env python3
"""Resolve MIPS64 GOT call loads in an owner ELF disassembly (read only)."""
import argparse
import re
from elftools.elf.elffile import ELFFile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('elf'); p.add_argument('disassembly')
p.add_argument('--gp', required=True, type=lambda v: int(v, 0))
a = p.parse_args()
with open(a.elf, 'rb') as source:
    elf = ELFFile(source)
    names = {}
    for section in elf.iter_sections():
        if section['sh_type'] in ('SHT_SYMTAB', 'SHT_DYNSYM'):
            for symbol in section.iter_symbols():
                if symbol['st_value']:
                    names.setdefault(symbol['st_value'], symbol.name)
    def pointer(address):
        for segment in elf.iter_segments():
            start = segment['p_vaddr']
            if start <= address and address + 8 <= start + segment['p_filesz']:
                source.seek(segment['p_offset'] + address - start)
                return int.from_bytes(source.read(8), 'little' if elf.little_endian else 'big')
        raise ValueError('GOT address is not file backed')
    for line in open(a.disassembly):
        match = re.search(r'ld\s+t9,(-?\d+)\(gp\)', line)
        if match:
            target = pointer(a.gp + int(match[1]))
            print(line.rstrip(), '=>', hex(target), names.get(target, '<unresolved>'))
