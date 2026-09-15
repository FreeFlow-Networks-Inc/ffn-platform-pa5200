#!/usr/bin/env python3
"""Summarize the pinned sysroot owner's verbose training log, without MMIO.

These are observed routine outcomes, not memory or offload qualification.
Keep failures even when subsequent algorithms clear the PHY error register.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re


START = re.compile(r'run_init_cal_algorithm - (\w+) \((FE100_DRAM_\w+)\) Running Init Calibration')
ERROR = re.compile(r'Error Register Value = (0x[0-9a-fA-F]+)')


def summarize(text):
    stages = []
    current = None
    for line in text.splitlines():
        match = START.search(line)
        if match:
            current = {'algorithm':match[1], 'interface':match[2],
                       'outcome':'incomplete', 'error_registers':[]}
            stages.append(current)
        if current is None:
            continue
        error = ERROR.search(line)
        if error:
            current['error_registers'].append(error[1].lower())
            current['outcome'] = 'failed'
        if re.search(r'run_init_cal_algorithm:\d+ failed-rc=', line):
            current['outcome'] = 'failed'
        if line.strip() == 'Exiting run_init_cal_algorithm.':
            if current['outcome'] != 'failed':
                current['outcome'] = 'returned_success'
            current = None
    return {'stages':stages, 'failed_algorithms':[s['algorithm'] for s in stages if s['outcome']=='failed'],
            'training_verified':False, 'session_offload_verified':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    args = parser.parse_args()
    raw = args.log.read_bytes()
    print(json.dumps(dict(summarize(raw.decode(errors='replace')),
                          log=str(args.log), log_sha256=hashlib.sha256(raw).hexdigest()), indent=2))


if __name__ == '__main__': main()
