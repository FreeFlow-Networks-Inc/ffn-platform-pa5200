#!/usr/bin/env python3
"""Live DP prerequisites for physical packet qualification; never enable DMA.

Exit2 means physical packet tests are blocked, not a successful hardware test.
This observer cannot prove end-to-end forwarding from ready bits alone.
"""
import json
from pathlib import Path
import sys
import time
import uuid
from ffn_dp_agent import snapshot


def main():
    live=snapshot(str(uuid.uuid4()))
    from ffn_dp_packet_init import status
    packet=status()
    root=Path('/sys/class/net')
    links=[]
    for path in sorted(root.iterdir()):
        links.append({'name':path.name,'physical_device':(path/'device').exists(),
                      'type':int((path/'type').read_text()),
                      'operstate':(path/'operstate').read_text().strip()})
    aw=int(packet['sso_aw_cfg'],16)
    checks={
        'debian_boot_ready':live.get('ready') is True,
        'internal_bgx_link_ready':live.get('packet_io',{}).get('internal_link_ready') is True,
        'dma_pools_prepared':packet.get('dma_ready') is True and packet.get('dma_error')==0,
        'pki_microcode_prepared':packet.get('pki_microcode_prepared') is True,
        'pki_enabled':packet.get('pki_enabled')==1,
        'sso_external_queue_operations_enabled':bool(aw&1),
        'pko_enabled':packet.get('pko_enabled')==1,
        'pko_queue_topology_prepared':packet.get('pko_queues_prepared') is True,
        'linux_physical_packet_interface_present':any(p['physical_device'] and p['type']==1 for p in links),
    }
    # Exercise the installed transport's real trunk gate against sysfs. A TAP
    # used for management must never be mistaken for a physical validation path.
    from ffn_dp_packet_transport import validate_trunk
    try:
        validate_trunk('ffndp0')
        management_rejected=False
    except ValueError:
        management_rejected=True
    result={'schema':1,'scope':'DP physical forwarding prerequisites',
        'boot_id':live['boot_id'],'sample_monotonic':time.monotonic(),
        'checks':checks,'packet_register_status':packet,'interfaces':links,
        'management_tap_rejected_as_trunk':management_rejected,
        'physical_forwarding_test':'not-run',
        'pki_rx_dma_test':'not-run','pko_tx_completion_test':'not-run',
        'session_hit_miss_forwarding_test':'not-run',
        'hardware_validated':False,
        'result':'blocked' if not all(checks.values()) else 'requires-traffic-test'}
    print(json.dumps(result,indent=2))
    return 2


if __name__=='__main__': sys.exit(main())
