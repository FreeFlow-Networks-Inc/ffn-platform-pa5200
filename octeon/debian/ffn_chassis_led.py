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


def decode_power(power):
    # Owner ehmon/cp/libpwrsupply.so: ps_monitor.c sample and descriptor.
    # Presence is active low; owner accepts ANY asserted bit in good_mask.
    supplies = []
    for name, led, absent_mask, good_mask in (
            ('left', 'ps0', 2, 0x30), ('right', 'ps1', 1, 0x0c)):
        present = not bool(power & absent_mask)
        good = present and bool(power & good_mask)
        supplies.append(dict(name=name, led=led, present=present, power_good=good,
                             state='absent' if not present else 'good' if good else 'fault'))
    return supplies


def access(updates=None):
    updates = updates or {}
    for name, color in updates.items():
        if name not in FIELDS or color not in COLORS:
            raise ValueError('invalid LED or color')
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
                for name, color in updates.items():
                    reg, shift = FIELDS[name]
                    mask = 3 << shift
                    expected = (csr[reg] & ~mask) | COLORS[color] << shift
                    csr[reg] = expected
                    if csr[reg] != expected:
                        raise RuntimeError('LED register readback failed')
                names = {v: k for k, v in COLORS.items()}
                power = csr[0x0a]
                return {'csr6': csr[6], 'csr7': csr[7], 'power_csr': power,
                    'power_supplies': decode_power(power),
                    'leds': {name: names.get((csr[reg] >> shift) & 3, 'reserved')
                             for name, (reg, shift) in FIELDS.items()}}
        finally:
            os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('led', nargs='?', choices=FIELDS)
    p.add_argument('color', nargs='?', choices=COLORS)
    a = p.parse_args()
    if bool(a.led) != bool(a.color):
        p.error('provide both LED and color, or neither for status')
    print(json.dumps(access({a.led: a.color} if a.led else None), indent=2))


if __name__ == '__main__':
    main()
