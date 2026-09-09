#!/usr/bin/env python3
"""Read non-clearing FE100 counters and block fault modes for lab evidence."""
import argparse
import json
import time
from ffn_fe100 import Fe100

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--compare', help='Earlier JSON snapshot; emit changed counters')
args = p.parse_args()
regs = json.load(open('/opt/ffn-compat/opt/ffn/fe100-csr.json'))
previous = json.load(open(args.compare))['registers'] if args.compare else {}
fe = Fe100()
values = {}
try:
    for r in regs:
        name = r['name']
        if name.endswith(('_no_rd_clr', '_cr_mode')):
            values[name] = fe.read32(r['addr'])
finally:
    fe.close()
result = {'time': time.time(), 'registers': values}
if args.compare:
    result['changes'] = {k: {'before': previous[k], 'after': v,
                            'delta_mod32': (v-previous[k]) & 0xffffffff}
                         for k, v in values.items() if k in previous and v != previous[k]}
print(json.dumps(result, indent=2))
