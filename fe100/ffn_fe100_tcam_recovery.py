#!/usr/bin/env python3
"""Bounded recovery of the cfg4 commissioning read; no table replay/reset."""
import json
from pathlib import Path
import time
from ffn_fe100_clocks import RST,INIT,CLOCK_MASK,TCAM_STATUS
from ffn_fe100_ddr import DDRRegisters
from ffn_fe100_external_runtime import ExternalTransport,JOURNAL,durable
from ffn_fe100_external_tables import cfg4_v4_v6
from ffn_fe100_tcam_sync import NOP_DONE,synchronize

PACKET_COUNTERS=(0x28090,0x30090,0x38090,0x50090,0x80220)


def require_idle_traffic(io,sleep=time.sleep):
    first=tuple(io.read(r) for r in PACKET_COUNTERS)
    sleep(.05)
    if tuple(io.read(r) for r in PACKET_COUNTERS)!=first:
        raise RuntimeError('packet/lookup traffic is active; recovery prohibited')


def recover(replay=False):
    io=DDRRegisters(True,training=True)
    transport=ExternalTransport(io)
    for addr in (0x80794,0x80200,0x80204,*PACKET_COUNTERS):
        if io.shim.ffn_fe100_allow_readonly(addr): raise RuntimeError('recovery read allowlist failed')
    for addr in (0xa0704,0xa0184,0xa0218,0xa0228):
        if io.shim.ffn_fe100_allow(addr): raise RuntimeError('recovery read allowlist failed')
    registers=(RST,INIT,0x80500,0x80504,0x80508,0x80794,0xa0704,0xa0184,0xa0214,0xa0224)
    def snapshot(): return {hex(r):io.read(r) for r in registers}
    record=json.loads(JOURNAL.read_text())
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if (record.get('stage') not in ('failed','recovery-failed') or record.get('completed')!=263 or
        record.get('profile')!='cfg4-v4-v6' or record.get('cp_boot_id')!=boot or
        record.get('session_offload_verified') is not False):
        raise RuntimeError('not the known same-boot commissioning failure')
    expected_address=record.get('recovery',{}).get('after',{}).get('0x80504',1)
    if expected_address not in {entry.address for entry in cfg4_v4_v6()}:
        raise RuntimeError('journal contains an unsupported recovery address')
    if (io.read(0x80500)&~1!=0x02000200 or io.read(0x80504)!=expected_address or
        io.read(0x80508) not in (0,0x800000)):
        raise RuntimeError('IA state differs from stalled cfg4 register read: '+json.dumps(snapshot()))
    require_idle_traffic(io)
    archive=JOURNAL.with_name('external-tables-before-recovery-'+str(time.time_ns())+'.json')
    durable(archive,record)
    record.pop('readback_mismatch',None)
    record.update(stage='recovering',recovery={'before':snapshot(),'trace':io.trace,
        'archive':str(archive),'method':'owner-nop-synchronization'})
    durable(JOURNAL,record)
    try:
        record['recovery']['synchronization']=synchronize(io)
        for _ in range(500):
            if (io.read(0x80508)>>23)&7: break
            time.sleep(.002)
        record['recovery']['after']=snapshot()
        if (io.read(0x80508)>>23)&7!=1 or not transport.quiescent():
            raise RuntimeError('stalled TCAM read did not complete and drain')
        transport.record=record
        if replay:
            if not io.read(INIT)&NOP_DONE:
                raise RuntimeError('replay requires verified NOP synchronization')
            record['recovery']['replay_completed']=0
            durable(JOURNAL,record)
            for entry in cfg4_v4_v6():
                record['recovery']['attempt_address']=entry.address
                durable(JOURNAL,record) # intent survives interruption before MMIO
                request,data=entry.native()
                if transport.ia_op(0,16,request): raise RuntimeError('replay write failed')
                if not transport.verify_configuration((entry,)):
                    raise RuntimeError('replay immediate readback failed at '+hex(entry.address))
                record['recovery']['replay_completed']+=1
                durable(JOURNAL,record)
            durable(JOURNAL,record)
        if not transport.verify_configuration(cfg4_v4_v6()):
            raise RuntimeError('TCAM configuration still fails readback')
        record.update(stage='configuration-verified',recovery_required=False)
        record['recovery']['after']=snapshot()
        record['verified_at_ns']=time.time_ns()
        durable(JOURNAL,record)
        return record
    except BaseException as error:
        record.update(stage='recovery-failed',recovery_required=True)
        record['recovery'].update(error=str(error),after=snapshot())
        durable(JOURNAL,record)
        raise


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover',action='store_true')
    parser.add_argument('--replay',action='store_true')
    args=parser.parse_args()
    if args.replay and not args.recover: parser.error('--replay requires --recover')
    if args.recover: print(json.dumps(recover(args.replay),indent=2))
    else:
        io=DDRRegisters(False,training=True)
        io.shim.ffn_fe100_allow_external_ia()
        regs=(0x80500,0x80504,0x80508,0x8050c,0x80510,0x80514,0xa0708,
              0xa01d4,0xa01e4,0xa01f4,0xa0214,0xa0224)
        print(json.dumps({hex(r):hex(io.read(r)) for r in regs}))
