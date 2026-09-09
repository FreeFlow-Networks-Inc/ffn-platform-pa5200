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
         'qsfp-advertise-40g': 27, 'qsfp-restore-advertisement': 28}
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('mode', choices=tuple(modes))
p.add_argument('output', type=pathlib.Path)
args = p.parse_args()
source = pathlib.Path(__file__).with_name('ffn_bcm_forward_test.c').read_text()
output, count = re.subn(r'int fe100_test = [0-9]+;',
                       'int fe100_test = %d;' % modes[args.mode], source)
if count != 1:
    raise SystemExit('expected one mode declaration; template changed')
args.output.write_text(output, encoding='utf-8', newline='\n')
print('Prepared %s in %s' % (args.mode, args.output))
