#!/usr/bin/env python3
"""Read the TDI DDR defaults from an operator-supplied FE100 ELF; no hardware I/O."""
import argparse
import hashlib
import json
import struct

OWNER_SHA='b57227a460144c8c2545fc2e268b31f475ef72ac2a6f1d457387ab46d842c3e9'
# Exact owner DWARF: fe100_cfg1.tdi_cnf.dram_cfg at 1576, 248 bytes.
FIELDS={
    'dimm_count':4,'dimm_capacity':16,'ddr_type':20,'dram_width':24,
    'dimm_type':28,'dram_vendor':32,'dram_size':36,'dram_ranks':40,
    'dram_speed_grade':44,'ddr4_rtt_nom':76,'ddr4_rtt_wr':80,
    'ddr4_dram_ods':84,'ddr4_dynamic_odt_enable':88,'interface':108,
    'run_init_cal_all':144,'enable_read_lvl':148,'enable_write_lvl':152,
    'enable_zq_calib':156,'enable_periodic_calib':160,'enable_init_calib':164,
    'enable_internal_calib':168,'enable_vref_training':184,
    'enable_init_pat_write':188,'enable_dqs_alignment':192,
    'enable_sysclk_rdclk_alignment':196,'enable_sysclk_nclk_alignment':200,
    'enable_write_centering':204,'enable_coarse_init_pat_write':208,
    'enable_coarse_read_alignment':212,
}


def decode(config):
    if len(config)!=2812: raise ValueError('owner configuration size changed')
    ddr=config[1576:1824]
    return {'schema':1,'source_sha256':OWNER_SHA,
        'owner_defaults':{k:struct.unpack_from('>I',ddr,o)[0] for k,o in FIELDS.items()},
        'vref':dict(zip(('initial_value','initial_range','final_value','final_range'),
                        struct.unpack_from('>4H',ddr,100))),
        'ddr_pll_words':list(struct.unpack_from('>5I',config,1492+60)),
        'runtime_eeprom_override':{'bus':0,'address':0x57,'tuple_type':0xf00c},
        'training_verified':False}


def extract(path):
    from elftools.elf.elffile import ELFFile
    with open(path,'rb') as f:
        if hashlib.file_digest(f,'sha256').hexdigest()!=OWNER_SHA:
            raise ValueError('unrecognized owner library')
        f.seek(0)
        elf=ELFFile(f)
        if elf.little_endian or elf.elfclass!=64: raise ValueError('expected MIPS64 BE ELF')
        symbols=elf.get_section_by_name('.dynsym').get_symbol_by_name('fe100_cfg1')
        if not symbols or symbols[0]['st_size']!=2812: raise ValueError('configuration ABI changed')
        sym=symbols[0]
        section=elf.get_section(sym['st_shndx'])
        offset=sym['st_value']-section['sh_addr']
        return decode(section.data()[offset:offset+2812])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('elf')
    print(json.dumps(extract(p.parse_args().elf),indent=2))
