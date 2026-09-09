#!/usr/bin/env python3
"""PA-5200 CP chassis LEDs; recovered from brdagent/cp/libmgmt.so.

Only CSR6/7 LED fields can be written. No reset or power-control writes.
Run on the CP. The MP can invoke this through ffn-cp.
"""
import argparse
import fcntl
import json
import mmap
import os
import struct

FIELDS = {'ps1': (6, 6), 'ps0': (6, 4), 'fans': (6, 2),
          'temp': (6, 0), 'status': (7, 4), 'alarm': (7, 2), 'ha': (7, 0)}
COLORS = {'off': 0, 'green': 1, 'yellow': 2}
BOOT_CFG2 = 0x1180000000010


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('led', nargs='?', choices=FIELDS)
    p.add_argument('color', nargs='?', choices=COLORS)
    a = p.parse_args()
    if bool(a.led) != bool(a.color):
        p.error('provide both LED and color, or neither for status')
    with open('/run/ffn-chassis-led.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        try:
            page = mmap.PAGESIZE
            with mmap.mmap(fd, page, offset=BOOT_CFG2 & -page) as cfg:
                value = struct.unpack_from('>Q', cfg, BOOT_CFG2 % page)[0]
            base = (value & 0xffff) << 16
            # This tool is specific to the validated PA-5220 boot-bus mapping.
            if base != 0x1b040000:
                raise RuntimeError('unrecognized CP CPLD mapping')
            with mmap.mmap(fd, page, offset=base) as csr:
                if csr[0] != 0x13:
                    raise RuntimeError('unrecognized CP CPLD version')
                if a.led:
                    reg, shift = FIELDS[a.led]
                    mask = 3 << shift
                    expected = (csr[reg] & ~mask) | COLORS[a.color] << shift
                    csr[reg] = expected
                    if csr[reg] != expected:
                        raise RuntimeError('LED register readback failed')
                names = {v: k for k, v in COLORS.items()}
                power = csr[0x0a]
                print(json.dumps({'csr6': csr[6], 'csr7': csr[7], 'power_csr': power,
                    'power_supplies': [
                        {'name': 'left', 'present': not bool(power & 2), 'power_good': bool(power & 0x30)},
                        {'name': 'right', 'present': not bool(power & 1), 'power_good': bool(power & 0x0c)}],
                    'leds': {name: names.get((csr[reg] >> shift) & 3, 'reserved')
                             for name, (reg, shift) in FIELDS.items()}}, indent=2))
        finally:
            os.close(fd)


if __name__ == '__main__':
    main()
