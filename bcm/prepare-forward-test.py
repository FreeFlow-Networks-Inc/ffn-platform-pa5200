#!/usr/bin/env python3
"""Render the fixed CINT lab recipe with an explicit test mode; no hardware IO."""
import argparse
import pathlib
import re

modes = {'front-allocate': 0, 'nif-allocate': 1, 'nif-enable': 2,
         'nif-disable': 3, 'il-allocate': 4, 'il-enable': 5, 'il-disable': 6,
         'il-capture-raw': 7, 'il-restore-tm': 8,
         'tm-front-allocate': 9, 'tm-front-disable': 10, 'front-fe-enable': 11,
         'tm-front13-allocate': 12, 'fabric-pair-enable': 13, 'fabric-pair-disable': 14,
         'tm-front3-allocate': 15, 'tm-front1-allocate': 16,
         'fabric-four-enable': 17, 'fabric-four-disable': 18, 'led-inspect': 19,
         'autoneg-inspect': 20, 'autoneg-enable': 21, 'autoneg-disable': 22,
         'tm-front23-allocate': 23, 'tm-front24-allocate': 24,
         'qsfp-test-enable': 25, 'qsfp-test-disable': 26,
         'qsfp-advertise-40g': 27, 'qsfp-restore-advertisement': 28,
         'offload-rule-create': 29, 'offload-rule-delete': 30, 'offload-rule-counters': 31,
         'offload-front1-release': 32, 'offload-front1-restore': 33,
         'offload-raw-rule-create': 34, 'offload-tm-rule-create': 35}
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('mode', choices=tuple(modes))
p.add_argument('output', type=pathlib.Path)
for item in ('group', 'entry', 'stat', 'dq1', 'dq2', 'presel'):
    p.add_argument('--hw-'+item, type=int, default=-1)
args = p.parse_args()
source = pathlib.Path(__file__).with_name('ffn_bcm_forward_test.c').read_text()
output, count = re.subn(r'int fe100_test = [0-9]+;',
                       'int fe100_test = %d;' % modes[args.mode], source)
if count != 1:
    raise SystemExit('expected one mode declaration; template changed')
for item in ('group', 'entry', 'stat', 'dq1', 'dq2', 'presel'):
    value = getattr(args, 'hw_'+item)
    if value < 0 and ((modes[args.mode] == 31 and item in ('group','entry','stat')) or (modes[args.mode] == 30 and item in ('group','entry'))):
        p.error('cleanup/counters requires the IDs returned by offload-rule-create')
    output = output.replace('int hw_%s = -1;' % item, 'int hw_%s = %d;' % (item, value))
args.output.write_text(output, encoding='utf-8', newline='\n')
print('Prepared %s in %s' % (args.mode, args.output))
