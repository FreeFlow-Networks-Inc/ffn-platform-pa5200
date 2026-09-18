#!/usr/bin/env python3
"""Read-only FE100 commissioning status for the optional PA-5200 provider."""
import json
from pathlib import Path
import time
from ffn_fe100_ddr import DDRRegisters, DPHY_STATUS, DDR_STATUS, DDR_CLOCK_MASK, FIFO_STATUS
from ffn_fe100_clocks import TCAM_STATUS, INIT, CLOCK_MASK, RST
from ffn_fe100_external_runtime import JOURNAL
from ffn_fe100_tcam_sync import NOP_DONE


def journal_health(journal,boot,clocks_ready,synchronized):
    if journal is None: return {'recovery_required':False,'configuration_readback_recorded':False}
    verified=(journal.get('stage')=='configuration-verified' and
              journal.get('cp_boot_id')==boot and clocks_ready and synchronized)
    return {'recovery_required':not verified,'configuration_readback_recorded':bool(verified)}


def observe():
    io=DDRRegisters(False,training=True)
    state=io.snapshot()
    journal=json.loads(JOURNAL.read_text()) if JOURNAL.exists() else None
    memory=Path('/var/lib/ffn/fe100/ddr-memory-verified.json')
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    queues={hex(r):io.read(r) for r in FIFO_STATUS}
    health=journal_health(journal,boot,bool(io.read(TCAM_STATUS)&1) and
                          io.read(INIT)&CLOCK_MASK==CLOCK_MASK,bool(io.read(INIT)&NOP_DONE))
    return {'schema':1,'provider':'pa5200','observed_at':time.time(),'cp_boot_id':boot,
        'ddr_training_verified':io.read(DPHY_STATUS)&0x410==0x410 and io.read(RST)&0x4800==0x4800,
        'ddr_clocks_ready':bool(io.read(DDR_STATUS)&1) and io.read(INIT)&DDR_CLOCK_MASK==DDR_CLOCK_MASK,
        'tcam_clocks_ready':bool(io.read(TCAM_STATUS)&1) and io.read(INIT)&CLOCK_MASK==CLOCK_MASK,
        'memory_test_history':json.loads(memory.read_text()) if memory.exists() else None,
        'external_tables':journal,
        **health,'tcam_synchronized':bool(io.read(INIT)&NOP_DONE),
        # Even successful historical readback cannot prove persistence across
        # a device reset within the same CP boot. Live lookups must qualify it.
        'external_tables_qualified':False,'session_backend_qualified':False,
        'forwarding_verified':False,'queue_status':queues,'registers':state,
        'supported_actions':['status'],
        'blocked_actions':{'activate':'TCAM completion/readback and packet tests required',
                           'session-install':'hardware flow-entry adapter unqualified'}}


if __name__=='__main__': print(json.dumps(observe()))
