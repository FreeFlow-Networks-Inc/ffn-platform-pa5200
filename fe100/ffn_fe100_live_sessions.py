#!/usr/bin/env python3
"""Locked, bounded FE100 session endpoint for the PA-5220 CP.

Status is read-only. Opening a session endpoint never initializes or resets
hardware. Failed calibration journals override apparent DPHY init-done bits.
Production NativeSessionAdapter retains its separate packet qualification gate.
"""
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
from ffn_fe100_clocks import LIB, SHA
from ffn_fe100_config import load_profile, native_configuration
from ffn_fe100_session_adapter import CtypesOwnerEndpoint

ROOT = Path('/var/lib/ffn/fe100')
HEALTH = (0x40010,0x40014,0x40400,0x40404,0x40428,0x40450,0x48008,0x48018,0x48708,
          0xa8100,0xa8104,0xa8134,0xa8148,0xa8150,0xa8168,0xa8170,
          0xb0100,0xb0104,0xb0134,0xb0148,0xb0150,0xb0168,0xb0170)
ACTION_HEALTH = (0x98008,0x98174,0x98128,0x98130,0x98148,0x98150,
                 0x78134,0x78804,0x406a4)


def action_prerequisites(values, root, boot):
    """FLOWUPDATE needs trained counter memory, beyond hash-table readiness.

    Native completion alone does not report CFP FCM allocation failure.
    Require this boot's commissioning records and current hardware state.
    """
    reasons = []
    for stage in ('fcm-clocks','fcm-train-0','fcm-train-1','sem-init'):
        try:
            record = json.loads((root/(stage+'-'+boot+'.json')).read_text())
            valid = (record.get('cp_boot_id') == boot and record.get('stage') == 'completed'
                     and record.get('owner_sha256') == SHA and record.get('faults') == 0)
        except (OSError, ValueError, AttributeError):
            valid = False
        if not valid: reasons.append(stage+' lacks successful initialization in this boot')
    if values.get(0x98008,0)&3 != 3 or values.get(0x98174,0)&1 != 1:
        reasons.append('FCM clocks are not ready')
    for channel in (0,1):
        if (values.get(0x98128+32*channel,0)&0x410 != 0x410 or
                values.get(0x98130+32*channel,0)&7 != 1):
            reasons.append('FCM'+str(channel)+' controller is not ready')
    if values.get(0x78804,0)&0x1000e0 != 0x1000e0:
        reasons.append('SEM initialization is incomplete')
    if not values.get(0x406a4,0)&255:
        reasons.append('FLU has no free counter IDs')
    return reasons


def calibration_journals(root, boot):
    """Later recovery stages supersede training, including failed attempts.

    Fixed recovery ordering follows the enforced commissioning sequence. Do
    not select only successful records or depend on adjustable wall clocks.
    A damaged latest journal also blocks activation rather than falling back.
    """
    records = {}
    for block, channels in (('fhm',(3,4)),('fdt',(5,6))):
        for channel in channels:
            stages = ['train-'+str(channel)]
            if channel == 6:
                stages += ['recover-calibration-6','recover-controller-6','recover-init-pattern-6']
            for stage in stages:
                path = root/(block+'-'+stage+'-'+boot+'.json')
                try:
                    raw = path.read_text()
                except FileNotFoundError:
                    continue
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict): raise ValueError('expected object')
                except (ValueError, TypeError):
                    record = {'stage':'invalid','error':'invalid calibration journal'}
                records[channel] = dict(record, journal=str(path))
    return records


def prerequisites(values, boot, journals):
    reasons = []
    for block, base, channels in (('fhm',0xa8000,(3,4)), ('fdt',0xb0000,(5,6))):
        if values.get(base+0x134,0)&1 != 1 or values.get(base+0x100,0)&3 != 3:
            reasons.append(block+' clocks are not ready')
        for slot, channel in enumerate(channels):
            record = journals.get(channel,{})
            if (record.get('cp_boot_id') != boot or record.get('stage') != 'completed' or
                    record.get('owner_sha256') != SHA or record.get('faults') != 0):
                reasons.append(block+str(slot)+' lacks successful calibration in this boot')
            if values.get(base+0x148+32*slot,0)&0x410 != 0x410:
                reasons.append(block+str(slot)+' DPHY is not ready')
            if values.get(base+0x150+32*slot,0)&7 != 1:
                reasons.append(block+str(slot)+' controller is not in normal mode')
    # Sysroot cfg4 initializes targets1..0x400 (11 bits), not all20 bits.
    if values.get(0x40010,0)&0x7ff != 0x7ff:
        reasons.append('FLU memory initialization is incomplete')
    if values.get(0x40014) != 0:
        reasons.append('FLU is not configured for the audited usecase')
    # Native initialization clears CPU-access mode before normal operation.
    # The owner does not program the legacy 40404 reset fields; actual DDR
    # controller readiness is checked above at the FHM/FDT registers.
    if values.get(0x48708,0)&7 != 7 or not values.get(0x48018,0)&1:
        reasons.append('CFP session processor is not ready')
    return reasons


def flu_record(root, boot):
    path=root/('flu-verified-'+boot+'.json')
    if not path.exists(): path=root/('flu-init-'+boot+'.json')
    try:
        record=json.loads(path.read_text())
        if (record.get('cp_boot_id')==boot and record.get('owner_sha256')==SHA and
                record.get('stage')=='completed' and record.get('faults')==0):
            return record
    except (OSError,ValueError,AttributeError): pass
    return None


class LiveSessions:
    def __init__(self, writable=False, lock_fd=None, commissioning=False):
        from ffn_fe100 import bar0_base_and_size, memory_decode_on
        if sys.byteorder != 'big' or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError('requires CP MIPS64 big-endian ABI')
        if lock_fd is not None:
            if os.readlink('/proc/self/fd/'+str(lock_fd)) != '/run/ffn-fe100-tables.lock':
                raise RuntimeError('invalid inherited table lock')
            self.lock = os.fdopen(os.dup(lock_fd), 'a')
        else:
            self.lock = open('/run/ffn-fe100-tables.lock','a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if hashlib.sha256(Path(LIB).read_bytes()).hexdigest() != SHA:
            raise RuntimeError('owner ABI changed')
        base,size = bar0_base_and_size()
        if size != 0x100000 or not memory_decode_on():
            raise RuntimeError('FE100 BAR unavailable')
        self.shim = C.CDLL('/usr/local/lib/ffn/libffn-fe100-flow-memory.so',mode=os.RTLD_GLOBAL|os.RTLD_NOW)
        if self.shim.ffn_fe100_select_block(0x40000) or self.shim.ffn_flow_session_mode():
            raise RuntimeError('session scope selection failed')
        import time
        self.trace = str(ROOT/('session-io-'+str(time.time_ns())+'.txt'))
        self.shim.ffn_fe100_open.argtypes = [C.c_uint64,C.c_char_p,C.c_int]
        if self.shim.ffn_fe100_open(base,self.trace.encode(),writable):
            raise RuntimeError('session mapping failed')
        # Only session IA windows and CPU start control can be written.
        for r in (*range(0x40800,0x40890,4),0x48014,*range(0x486c0,0x486f4,4)):
            if r not in (0x4080c,0x486c8): self.shim.ffn_fe100_allow(r)
        for r in (*HEALTH,*ACTION_HEALTH,0x4080c,0x486c8): self.shim.ffn_fe100_allow_readonly(r)
        self.shim.fe100_reg_rd.argtypes = [C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32)]
        self.writable = writable
        self.commissioning = commissioning
        self.endpoint = None
        if writable:
            state = self.status()
            reasons=state['commissioning_blockers'] if commissioning else state['blockers']
            if reasons: raise RuntimeError('; '.join(reasons))
            self.lib = C.CDLL(LIB,mode=os.RTLD_LOCAL|os.RTLD_LAZY)
            self.cfg = C.create_string_buffer(native_configuration(bytes((C.c_char*2812).in_dll(self.lib,'fe100_cfg1')),load_profile()),2812)
            # Exact ELF DWARF: eight fe100_dev_t objects, stride272, config
            # pointer at264. Native session calls read usecase through it.
            # Retain both objects for endpoint lifetime. No device init call.
            self.devices = (C.c_ubyte*2176).in_dll(self.lib,'fe100_dev')
            C.c_void_p.from_buffer(self.devices,264).value = C.addressof(self.cfg)
            self.endpoint = CtypesOwnerEndpoint(self.lib,self.status,self._invoke)

    def read(self,r):
        value = C.c_uint32()
        if self.shim.fe100_reg_rd(0,r,C.byref(value)):
            raise RuntimeError('session register read failed: '+hex(r))
        return value.value

    def status(self):
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        journals = calibration_journals(ROOT, boot)
        values = {r:self.read(r) for r in (*HEALTH,*ACTION_HEALTH)}
        blockers = prerequisites(values,boot,journals)
        if flu_record(ROOT,boot) is None:
            blockers.append('FLU lacks verified initialization in this boot')
        actions=action_prerequisites(values,ROOT,boot)
        warm=False
        if self.commissioning:
            from ffn_fe100_warm import grant_valid
            warm=grant_valid(ROOT,boot,values)
        return {'owner_sha256':SHA,'device':0,'initialized':not blockers,
                'exclusive':True,'bounded':True,'writable':self.writable,
                'cp_boot_id':boot,'blockers':blockers,
                'action_blockers':actions,
                'warm_lab_verified':warm,
                'commissioning_blockers':[] if warm else blockers+actions,
                'registers':{hex(r):v for r,v in values.items()},
                'calibration':{str(c):{k:r.get(k) for k in ('stage','error','journal')} for c,r in journals.items()},
                'session_offload_verified':False,'trace':self.trace}

    def _invoke(self,fn,entry):
        self.shim.ffn_flow_watchdog(10)
        try: rc = fn(0,C.byref(entry))
        finally: self.shim.ffn_flow_watchdog(0)
        if self.shim.ffn_fe100_faults():
            raise RuntimeError('session register scope violation')
        return rc

    def call(self,operation,native):
        if not self.writable or self.endpoint is None:
            raise RuntimeError('read-only session endpoint')
        state = self.status()
        reasons=state['commissioning_blockers'] if self.commissioning else state['blockers']
        if reasons:
            raise RuntimeError('hardware session prerequisites changed')
        if self.commissioning and (len(native)!=144 or int.from_bytes(native[18:20],'big') not in (4093,4094)):
            raise RuntimeError('commissioning session outside reserved lab zones')
        if operation == 'update' and state['action_blockers'] and not (self.commissioning and state['warm_lab_verified']):
            raise RuntimeError('; '.join(state['action_blockers']))
        return self.endpoint.call(operation,native)


if __name__ == '__main__':
    print(json.dumps(LiveSessions().status(),indent=2))
