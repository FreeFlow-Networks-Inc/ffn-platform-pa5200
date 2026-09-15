#!/usr/bin/env python3
"""Read-only BCM8375 LED processor diagnostics on the PA-5220 CP."""
import ctypes
import fcntl
import hashlib
import json
from pathlib import Path
import time

# PA-5220 owner jer.soc programs, padded to the 256-word program RAM.
REFERENCE_HASHES = (
    'd912a4aa64943079c40cb81e15010833f9c58072c11d3ee07a1fec0a1f706fe0',
    'e858b550a553cf572295d6eee546be1e27d43d910cb20671c75fa45f54ff430b',
    'c27af4ba6d15d2b777a645ff25ca66e508f271540be49848eaa3a6aa2ef597e6',
)


def reference_mismatches(processors):
    """Compare static configuration only; link/activity and PCs are dynamic."""
    mismatches = []
    for p in processors:
        index = p['index']
        expected = {'control': (0x20b, 0x28b, 0x28b)[index],
                    'clock_divider': 72, 'refresh_period': 21600000,
                    'assembly_start': 0x80, 'scanout_count_upper': 0,
                    'tm_control': 0, 'program_sha256': REFERENCE_HASHES[index]}
        for field, value in expected.items():
            if p[field] != value:
                mismatches.append({'processor': index, 'field': field,
                                   'expected': value, 'actual': p[field]})
    return mismatches


def snapshot():
    dev = Path('/sys/bus/pci/devices/0001:01:00.0')
    if ((dev / 'vendor').read_text().strip(), (dev / 'device').read_text().strip()) != ('0x14e4', '0x8375'):
        raise RuntimeError('unexpected switch identity')
    with open('/run/ffn-port-led.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        lib = ctypes.CDLL('/usr/local/lib/libffn-led-diagnostic.so')
        if lib.led_open():
            raise RuntimeError('LED register mapping failed')
        try:
            def read(offset):
                value = ctypes.c_uint32()
                if lib.led_read(offset, ctypes.byref(value)):
                    raise RuntimeError('LED diagnostic read rejected')
                return value.value
            processors = []
            for index, base in enumerate((0x20000, 0x21000, 0x29000)):
                ctrl, status = read(base), read(base + 4)
                code = bytes(read(base + 0x800 + 4*i) & 255 for i in range(256))
                remap = [read(base + 0x10 + 4*i) for i in range(16)]
                processors.append({'index': index, 'control': ctrl, 'enabled': bool(ctrl & 1),
                    'status': status, 'pc': status & 255, 'executing': bool(status & 0x100),
                    'initializing': bool(status & 0x200),
                    'clock_divider': read(base + 0x5c), 'refresh_period': read(base + 0x50),
                    'assembly_start': read(base + 8),
                    'scanout_count_upper': read(base + 0x54), 'tm_control': read(base + 0x58),
                    'program_sha256': hashlib.sha256(code).hexdigest(),
                    'input_remap_words': remap,
                    'input_remap_slots': [(word >> shift) & 63 for word in remap
                                          for shift in (0, 6, 12, 18)],
                    'packed_output': [read(base + 0x600 + 4*i) for i in range(8)],
                    'link_slots_set': [i for i in range(64) if read(base + 0x680 + 4*i) & 1]})
            return {'collected_at': time.time(), 'processors': processors,
                    'reference_mismatches': reference_mismatches(processors),
                    'qualification': 'Processor execution/output memory only; physical light operation unverified. executing=false between refreshes is normal.'}
        finally:
            lib.led_close()


if __name__ == '__main__':
    print(json.dumps(snapshot(), indent=2))
