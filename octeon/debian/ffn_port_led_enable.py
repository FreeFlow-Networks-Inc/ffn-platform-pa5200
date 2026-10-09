#!/usr/bin/env python3
"""Enable PA-5220 front-port LED output without changing BCM/PHY state.

Owner libports.so init sets CP CPLD CSR8 bit0 after BCM initialization.
Observed on PA-5220: setting this bit illuminates linked ports 5/13/23/24.
The other CSR8 bits are preserved. This does not program activity patterns.
"""
import fcntl
import argparse
import json
import mmap
import os
from pathlib import Path
import struct


def status(csr):
    if csr[0] != 0x13:
        raise RuntimeError('unrecognized CP CPLD version')
    return {'csr8': csr[8], 'output_enabled': bool(csr[8] & 1)}


def enable(csr):
    status(csr)
    before = csr[8]
    csr[8] = before | 1
    after = csr[8]
    if after != before | 1:
        raise RuntimeError('front-port LED enable readback failed')
    return {'csr8_before': before, 'csr8_after': after, 'output_enabled': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true', help='Read the output gate without writing hardware')
    args = parser.parse_args()
    dev = Path('/sys/bus/pci/devices/0001:01:00.0')
    if ((dev / 'vendor').read_text().strip(), (dev / 'device').read_text().strip()) != ('0x14e4', '0x8375'):
        raise RuntimeError('unexpected switch identity')
    with open('/run/ffn-chassis-led.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd = os.open('/dev/mem', (os.O_RDONLY if args.status else os.O_RDWR) | os.O_SYNC)
        try:
            addr = 0x1180000000010
            page = mmap.PAGESIZE
            with mmap.mmap(fd, page, offset=addr & -page, prot=mmap.PROT_READ) as cfg:
                bootcfg = struct.unpack_from('>Q', cfg, addr % page)[0]
            base = (bootcfg & 0xffff) << 16
            if base != 0x1b040000:
                raise RuntimeError('unrecognized CP CPLD mapping')
            with mmap.mmap(fd, page, offset=base, prot=mmap.PROT_READ if args.status else mmap.PROT_READ | mmap.PROT_WRITE) as csr:
                print(json.dumps(status(csr) if args.status else enable(csr)))
        finally:
            os.close(fd)


if __name__ == '__main__':
    main()
