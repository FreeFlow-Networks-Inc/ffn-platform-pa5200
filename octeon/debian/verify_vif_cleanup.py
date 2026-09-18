#!/usr/bin/env python3
"""Read-only post-test state and hardware queue checks on the DP."""
import json
from pathlib import Path
import subprocess
from ffn_dp_packet_init import status
config=json.loads(Path('/etc/ffn/vifs.json').read_text())
network=json.loads(Path('/etc/ffn/network.json').read_text())
links=json.loads(subprocess.check_output(['ip','-n','ffn-data','-j','link'],text=True))
vifs=[p['ifname'] for p in links if p.get('ifalias','').startswith('ffn:vif:')]
service=subprocess.run(['systemctl','is-active','ffn-vif.service'],capture_output=True,text=True).stdout.strip()
hardware=status()
result={'vif_config':config,'vif_netdevices':vifs,'vif_service':service,
        'recovery_pending':Path('/etc/ffn/vifs.pending.json').exists(),
        'network_config':network,'packet_hardware':hardware,
        'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
assert not config['vifs'] and not vifs and service=='inactive' and not result['recovery_pending'],result
assert hardware['trunk']['bad_dma']==0 and hardware['trunk']['tx_rejected']==0,result
print(json.dumps(result))
