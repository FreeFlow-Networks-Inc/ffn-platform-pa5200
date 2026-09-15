#!/usr/bin/env python3
"""Bounded CP LED-only probe; restore and verify all three programs on exit."""
import ctypes
import fcntl
import json
from pathlib import Path
import signal
import time

BASES = (0x20000, 0x21000, 0x29000)


def program(bits, one):
    if bits not in (8, 24, 64) or type(one) is not bool:
        raise ValueError('unsupported probe pattern')
    # LED ISA: pushst constant zero/one, pack; send packed bit count.
    return [0x32, 0x0f if one else 0x0e, 0x87] * bits + [0x3a, bits]


def main():
    dev = Path('/sys/bus/pci/devices/0001:01:00.0')
    if (dev / 'vendor').read_text().strip() != '0x14e4' or (dev / 'device').read_text().strip() != '0x8375':
        raise RuntimeError('unexpected switch')
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with open('/run/ffn-port-led.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lib = ctypes.CDLL('/usr/local/lib/libffn-led-diagnostic.so')
        if lib.led_open():
            raise RuntimeError('cannot map LED registers')
        def read(offset):
            value = ctypes.c_uint32()
            if lib.led_read(offset, ctypes.byref(value)):
                raise RuntimeError('LED read rejected')
            return value.value
        def write(offset, value):
            if lib.led_write(offset, value):
                raise RuntimeError('LED write rejected')
        saved = []
        try:
            saved = [(b, read(b), [read(b + 0x800 + 4*i) for i in range(256)]) for b in BASES]
            if [c for _, c, _ in saved] != [0x20b, 0x28b, 0x28b]:
                raise RuntimeError('unrecognized LED controls')
            Path('/run/ffn-port-led-probe-before.json').write_text(json.dumps(saved))
            for bits in (8, 24, 64):
                for one, seconds in ((True, 15), (False, 5)):
                    code = program(bits, one)
                    for b, ctrl, _ in saved:
                        write(b, ctrl & ~1)
                        for i, value in enumerate(code):
                            write(b + 0x800 + 4*i, value)
                            if read(b + 0x800 + 4*i) != value:
                                raise RuntimeError('probe program readback mismatch')
                        write(b, ctrl)
                    time.sleep(.1)
                    print(json.dumps({'bits': bits, 'one': one, 'seconds': seconds,
                        'processors': [{'base':hex(b), 'status':read(b+4),
                            'packed':[read(b+0x600+4*i) for i in range(8)]} for b in BASES]}),flush=True)
                    time.sleep(seconds)
        finally:
            failures = []
            for b, ctrl, code in saved:
                try:
                    write(b, ctrl & ~1)
                    for i, value in enumerate(code):
                        write(b + 0x800 + 4*i, value)
                    write(b, ctrl)
                    if read(b) != ctrl or [read(b + 0x800 + 4*i) for i in range(256)] != code:
                        raise RuntimeError('restore readback mismatch')
                except Exception as e:
                    failures.append('%x: %s' % (b, e))
            lib.led_close()
            print(json.dumps({'restored': not failures, 'errors': failures}),flush=True)
            if failures:
                raise RuntimeError('LED restoration failed: ' + '; '.join(failures))


if __name__ == '__main__':
    main()
