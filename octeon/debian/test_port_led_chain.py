#!/usr/bin/env python3
"""Bounded LED-only serial-chain test on BCM8375; restores original microcode."""
import fcntl
import ctypes
import json
from pathlib import Path
import signal
import time


def main():
    dev = Path('/sys/bus/pci/devices/0001:01:00.0')
    assert (dev / 'vendor').read_text().strip() == '0x14e4'
    assert (dev / 'device').read_text().strip() == '0x8375'
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with open('/run/ffn-port-led.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lib = ctypes.CDLL('/usr/local/lib/libffn-led-mmio.so')
        if lib.led_open() != 0:
            raise RuntimeError('cannot map LED registers')
        try:
            def read(offset):
                value = ctypes.c_uint32()
                if lib.led_read(offset, ctypes.byref(value)):
                    raise RuntimeError('LED read rejected')
                return value.value
            def write(offset, value):
                if lib.led_write(offset, value):
                    raise RuntimeError('LED write rejected')
            bases = (0x20000, 0x21000, 0x29000)
            saved = [(b, read(b), [read(b + 0x800 + 4*i) for i in range(256)]) for b in bases]
            Path('/run/ffn-port-led-before.json').write_text(json.dumps(saved))
            try:
                for phase in range(12):
                    # pushst ZERO/ONE, pack (eight times), send 8 bits.
                    program = [0x32, 0x0e + phase % 2, 0x87] * 8 + [0x3a, 8]
                    for b, ctrl, _ in saved:
                        write(b, ctrl & ~1)
                        for i, value in enumerate(program):
                            write(b + 0x800 + 4*i, value)
                        write(b, ctrl | 1)
                    print('LED_CHAIN_PHASE', phase, phase % 2, flush=True)
                    time.sleep(5)
            finally:
                for b, ctrl, program in saved:
                    write(b, ctrl & ~1)
                    for i, value in enumerate(program):
                        write(b + 0x800 + 4*i, value)
                    write(b, ctrl)
                    if read(b) != ctrl:
                        raise RuntimeError('LED control restore failed')
                    if [read(b + 0x800 + 4*i) for i in range(256)] != program:
                        raise RuntimeError('LED program restore failed')
                print('LED_CHAIN_RESTORED', flush=True)
        finally:
            lib.led_close()


if __name__ == '__main__':
    main()
