#!/usr/bin/env python3
"""Exclusive, journaled PA-5220 external TCAM commissioning transport."""
import ctypes as C
import json
import os
from pathlib import Path
import time

from ffn_fe100_ddr import DDRRegisters, DPHY_STATUS, FIFO_STATUS, verify_memory
from ffn_fe100_clocks import TCAM_STATUS, INIT, CLOCK_MASK
from ffn_fe100_external_tables import IndirectAccess, initialize, cfg4_v4_v6
from ffn_fe100_tcam_sync import NOP_DONE,synchronize

JOURNAL=Path('/var/lib/ffn/fe100/external-tables.json')


def durable(path, value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(value,output,indent=2); output.flush(); os.fsync(output.fileno())
    os.replace(temporary,path)
    fd=os.open(str(path.parent),os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


class ExternalTransport:
    def __init__(self,io,journal=JOURNAL):
        self.io=io
        self.journal=journal
        self.memory_verified=False
        self.record={}
        if io.shim.ffn_fe100_allow_external_ia():
            raise RuntimeError('external IA bridge unavailable')
        for addr in (0x80794,):
            if io.shim.ffn_fe100_allow_readonly(addr): raise RuntimeError('TLU observer unavailable')
        for addr in (0xa0218,0xa0228):
            if io.shim.ffn_fe100_allow(addr): raise RuntimeError('receive FIFO observer unavailable')
        self.fn=io.lib.fe100_ia_op
        self.fn.argtypes=[C.c_uint32,C.c_uint32,C.POINTER(IndirectAccess)]
        self.fn.restype=C.c_int

    def quiescent(self):
        # Require empty queues in two samples; forwarding stays disabled.
        lane_levels=[]
        for _ in range(2):
            if self.io.read(0xa0708)&3!=3:
                return False
            if self.io.read(0x80794)&0x1ff:
                return False
            if any(self.io.read(r)&0x1ff for r in FIFO_STATUS[1:] if r not in (0xa0214,0xa0224)):
                return False
            # Serial receive lanes retain alignment words after synchronization.
            # They sample a different clock domain and may alternate5/6 at
            # idle. Check capacity/pointer faults and actual work queues.
            if any(self.io.read(r)&0xc000c000 for r in (0xa0218,0xa0228)):
                return False
            lane_levels.append(tuple(self.io.read(r)&31 for r in (0xa0214,0xa0224)))
            time.sleep(.01)
        return all(v<16 for sample in lane_levels for v in sample)

    def status(self):
        return {'exclusive':True,
            'ddr_training_verified':self.io.read(DPHY_STATUS)&0x410==0x410,
            'ddr_memory_test_verified':self.memory_verified,
            'tcam_ready':bool(self.io.read(TCAM_STATUS)&1) and
                         self.io.read(INIT)&(CLOCK_MASK|NOP_DONE)==CLOCK_MASK|NOP_DONE,
            'packet_lookup_quiescent':self.quiescent(),
            'initialization_started':self.journal.exists()}

    def begin(self,profile,count):
        self.record={'stage':'initializing','profile':profile,'expected':count,
            'completed':0,'cp_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'trace':self.io.trace,'started_ns':time.time_ns(),
            'session_offload_verified':False}
        durable(self.journal,self.record)

    def ia_op(self,dev,block,request):
        if dev!=0 or block!=16 or request.trgt_mem!=0x100:
            raise ValueError('unsupported external table operation')
        rc=self.fn(dev,block,C.byref(request))
        if self.io.shim.ffn_fe100_faults():
            raise RuntimeError('external table MMIO denied')
        # IA completion code 1 means successful completion (CSR bits25:23).
        # Reject even an accidental zero return from a timed-out owner helper.
        return rc if rc else (0 if request.cc==1 else 12)

    def verify_configuration(self,entries):
        for entry in entries:
            request,data=entry.native()
            request.acc_type=1
            for i in range(3): data[i]=0
            rc=self.ia_op(0,16,request)
            if rc or tuple(data)!=entry.words:
                self.record['readback_mismatch']={'address':entry.address,
                    'expected':entry.words,'actual':list(data),
                    'return_code':rc,'completion_code':request.cc}
                return False
        return self.quiescent()

    def complete(self,count):
        self.record.update(stage='configuration-verified',completed=count,
                           recovery_required=False,verified_at_ns=time.time_ns())
        durable(self.journal,self.record)

    def failed(self,count):
        self.record.update(stage='failed',completed=count)
        durable(self.journal,self.record)


def activate():
    io=DDRRegisters(True,training=True)
    transport=ExternalTransport(io)
    if JOURNAL.exists():
        raise RuntimeError('existing external-table journal requires recovery review')
    if not transport.quiescent(): raise RuntimeError('lookup queues not quiescent')
    synchronize(io)
    # Re-test memory in this same exclusive session, never trust a stale file.
    verify_memory(io)
    transport.memory_verified=True
    return initialize(transport)


def verify_live():
    """Read back the known configuration; never replay or mark sessions ready."""
    io=DDRRegisters(True,training=True) # IA reads require address/command writes.
    transport=ExternalTransport(io)
    record=json.loads(JOURNAL.read_text())
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if record.get('stage') not in ('configuration-verified','verification-failed') or record.get('cp_boot_id')!=boot:
        raise RuntimeError('same-boot verified configuration required')
    if io.read(INIT)&(CLOCK_MASK|NOP_DONE)!=CLOCK_MASK|NOP_DONE:
        raise RuntimeError('TCAM synchronization/clocks no longer ready')
    if io.read(0x80508)>>23&7!=1 or not transport.quiescent():
        raise RuntimeError('TCAM transaction still pending or queues not quiescent')
    transport.record=record
    try:
        if not transport.verify_configuration(cfg4_v4_v6()):
            raise RuntimeError('TCAM live configuration readback failed')
        record.update(stage='configuration-verified',verified_at_ns=time.time_ns(),verification_trace=io.trace,
                      recovery_required=False)
        durable(JOURNAL,record)
        return {'table_configuration_verified':True,'entries_verified':263,
                'recovery_required':False,'session_offload_verified':False,'trace':io.trace}
    except BaseException:
        record.update(stage='verification-failed',recovery_required=True,
                      verification_trace=io.trace)
        durable(JOURNAL,record)
        raise


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--activate',action='store_true')
    actions.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.activate: print(json.dumps(activate()))
    elif args.verify: print(json.dumps(verify_live()))
    else:
        print(json.dumps({'journal':json.loads(JOURNAL.read_text()) if JOURNAL.exists() else None,
                          'session_offload_verified':False,'hardware_applied':False}))
