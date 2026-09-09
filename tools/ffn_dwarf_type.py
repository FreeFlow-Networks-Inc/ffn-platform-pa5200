#!/usr/bin/env python3
"""Inspect an owner-supplied ELF's DWARF type at a verified DIE offset."""
import argparse
from elftools.elf.elffile import ELFFile
from elftools.dwarf.dwarf_expr import DWARFExprParser

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('elf')
p.add_argument('offset', type=lambda s: int(s, 0))
a = p.parse_args()
with open(a.elf, 'rb') as source:
    dwarf = ELFFile(source).get_dwarf_info()
    die = dwarf.get_DIE_from_refaddr(a.offset)
    def attr(d, name, default=None):
        v = d.attributes.get(name)
        return default if v is None else v.value
    def label(d):
        return attr(d, 'DW_AT_name', b'').decode(errors='replace')
    while die.tag in ('DW_TAG_typedef', 'DW_TAG_pointer_type', 'DW_TAG_const_type'):
        print(hex(die.offset), die.tag, label(die))
        die = die.get_DIE_from_attribute('DW_AT_type')
    print(hex(die.offset), die.tag, label(die), 'size', attr(die, 'DW_AT_byte_size'))
    for child in die.iter_children():
        loc = attr(child, 'DW_AT_data_member_location')
        if isinstance(loc, list):
            loc = [(op.op_name, op.args) for op in DWARFExprParser(dwarf.structs).parse_expr(loc)]
        target = child.get_DIE_from_attribute('DW_AT_type') if 'DW_AT_type' in child.attributes else None
        print(label(child), 'offset', loc, 'value', attr(child, 'DW_AT_const_value'),
              'type', hex(target.offset) if target else None)
