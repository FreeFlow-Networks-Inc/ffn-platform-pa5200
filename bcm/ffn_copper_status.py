#!/usr/bin/env python3
"""Read the four BCM84848 identities, firmware and link/control registers."""
import json
from ffn_mdio import Mdio

bus = Mdio()
try:
    for phy in range(16, 20):
        row = {'mdio_address': phy}
        for name, dev, reg in (
            ('id1', 1, 2), ('id2', 1, 3),
            ('firmware', 30, 0x400f), ('strap', 30, 0x401a),
            ('pma_control', 1, 0), ('pma_status', 1, 1),
            ('pcs_control', 3, 0), ('pcs_status', 3, 1),
            ('an_control', 7, 0), ('an_status', 7, 1),
            ('copper_control', 7, 0xffe0), ('copper_status', 7, 0xffe1),
        ):
            # Link bits can latch low; take the current second read.
            bus.transfer(phy, dev, reg)
            row[name] = '0x%04x' % bus.transfer(phy, dev, reg)
        print(json.dumps(row), flush=True)
finally:
    bus.close()
