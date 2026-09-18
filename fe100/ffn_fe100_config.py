#!/usr/bin/env python3
"""Validate FFN's FE100 profile and overlay a private native configuration.

File inspection only: no vendor import, device access, or initialization.
The '+' value denotes overrides of native defaults, never evaluated code.
"""
import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

CONFIG = Path('/etc/ffn/fe100.cfgdb.xml')
MAX_BYTES = 4096


@dataclass(frozen=True)
class Profile:
    usecase: int = 1
    cfg_mode: int = 4
    v4_v6_choice: int = 2

    def __post_init__(self):
        values=(self.usecase,self.cfg_mode,self.v4_v6_choice)
        if any(type(v) is not int for v in values) or values != (1,4,2):
            raise ValueError('only the audited PA-5220 profile (1,4,2) is supported')


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate FE100 setting: '+key)
        result[key]=value
    return result


def parse_profile(data):
    if not isinstance(data,bytes) or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('FE100 XML must be 1..4096 bytes')
    source=data.decode('utf-8')
    if '<!DOCTYPE' in source.upper() or '<!ENTITY' in source.upper():
        raise ValueError('XML DTDs and entities are unsupported')
    try:root=ET.fromstring(source)
    except ET.ParseError as exc:raise ValueError('invalid FE100 XML') from exc
    if root.tag!='configdb' or root.attrib or len(root)!=1:
        raise ValueError('expected one configdb entry')
    entry=root[0]
    if entry.tag!='entry' or entry.attrib!={'name':'hw.fe100'} or len(entry)!=1:
        raise ValueError('expected hw.fe100 entry')
    value=entry[0]
    if value.tag!='value' or value.attrib or len(value):
        raise ValueError('expected a scalar profile value')
    if any((s or '').strip() for s in (root.text,entry.tail,entry.text,value.tail)):
        raise ValueError('unexpected XML text')
    payload=(value.text or '').strip()
    if not payload.startswith('+'):raise ValueError('expected profile override prefix +')
    settings=json.loads(payload[1:],object_pairs_hook=unique_object)
    if not isinstance(settings,dict) or set(settings)!={'usecase','cfg_mode','v4_v6_choice'}:
        raise ValueError('exactly usecase, cfg_mode and v4_v6_choice are required')
    return Profile(**settings)


def load_profile(path=None):
    """Missing default path preserves the pre-profile audited settings.

    An explicitly requested file must exist. Malformed, unreadable, or
    unsupported installed files never fall back to defaults.
    """
    selected=CONFIG if path is None else Path(path)
    try:
        with selected.open('rb') as source:data=source.read(MAX_BYTES+1)
    except FileNotFoundError:
        if path is not None or selected.is_symlink():raise
        return Profile()
    return parse_profile(data)


def native_configuration(template,profile):
    """Overlay only three ABI fields; callers retain owner-hash/readiness gates."""
    if not isinstance(profile,Profile):raise ValueError('validated FE100 profile required')
    Profile(**asdict(profile))
    if len(template)!=2812:raise ValueError('unexpected FE100 native configuration size')
    result=bytearray(template)
    struct.pack_into('>I',result,0,profile.usecase)
    struct.pack_into('>II',result,1400,profile.cfg_mode,profile.v4_v6_choice)
    return bytes(result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,help='explicit file to validate; missing files are errors')
    args=parser.parse_args()
    try:profile=load_profile(args.config)
    except (ValueError,OSError) as exc:parser.error(str(exc))
    print(json.dumps({'settings':asdict(profile),'configuration_valid':True,
                      'hardware_applied':False,'session_offload_verified':False},indent=2))


if __name__=='__main__':main()
