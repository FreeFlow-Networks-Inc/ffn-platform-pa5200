#!/usr/bin/env python3
"""Clear latched FE100 pipeline error status before readiness is judged.

The FLU, CFP and PRW blocks each keep a ``cr.mode`` register (0x40200,
0x48080, 0x70500; reset value 0x4300) whose bits 12 (``fatal_err_stat``) and
13 (``non_fatal_err_stat``) are sticky, write-1-to-clear status. A fault that
happened before this boot's block initialisation (a dead DP, a CP restart
without an FE100 reset) stays latched through ``block-prw`` and the readiness
gate in ffn_fe100_live_sessions then blocks the whole lifecycle with "PRW
pipeline reports a latched hardware error", although the sequence itself ran
clean. 2026-10-14 on the PA-5220: PRW read 0x5300; writing the value back
cleared bit 12 and readiness passed.

This step runs once, right after the block initialisations, and writes each
register's current value back to itself: the configuration bits are read/write
and unchanged, the two status bits are write-1-to-clear. The values before and
after are printed for the init journal. A bit that is still set after the write
is a live fault, not stale history: the tool exits 1 and the init stops with
that message. The live-sessions observer never clears anything; only this
initialisation step does, and only in a boot that is re-initialising the blocks.
"""
import argparse
import json
import sys

PIPELINES = {0x40200: 'FLU', 0x48080: 'CFP', 0x70500: 'PRW'}
STATUS = 0x3000   # fatal_err_stat | non_fatal_err_stat (write-1-to-clear)


def observe(device):
    return {register: device.read32(register) for register in PIPELINES}


def clear(device, observe=observe):
    """Clear latched status on every pipeline; report before/after and what persists."""
    before = observe(device)
    for register, value in before.items():
        if value & STATUS:
            device.write32(register, value)
    after = observe(device)
    persisting = sorted(PIPELINES[r] for r, v in after.items() if v & STATUS)
    return {'before': {PIPELINES[r]: hex(v) for r, v in before.items()},
            'after': {PIPELINES[r]: hex(v) for r, v in after.items()},
            'cleared': sorted(PIPELINES[r] for r, v in before.items() if v & STATUS and not after[r] & STATUS),
            'persisting': persisting}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--clear', action='store_true', help='write the status bits back (default: observe only)')
    args = parser.parse_args(argv)
    from ffn_fe100 import Fe100, memory_decode_on
    if not memory_decode_on():
        raise SystemExit('FE100 memory decode is off')
    device = Fe100()
    try:
        if args.clear:
            report = clear(device)
        else:
            values = observe(device)
            report = {'before': {PIPELINES[r]: hex(v) for r, v in values.items()},
                      'persisting': sorted(PIPELINES[r] for r, v in values.items() if v & STATUS)}
    finally:
        device.close()
    print(json.dumps(report))
    if report['persisting']:
        print('latched pipeline error persists after clearing: ' + ', '.join(report['persisting']), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
