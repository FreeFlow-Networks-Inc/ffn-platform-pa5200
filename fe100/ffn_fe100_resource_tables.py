"""Bounded native FE100 resource table access under one inherited owner lock.

Only source-MAC and DIRECT next-hop APIs are exposed. Commissioned pools must
be supplied by the hardware owner; there is no default allocation range.
"""
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,'/usr/local/lib/ffn')
from session_stream import Lines,send
from ffn_fe100_nexthop import LIB,SHA

LOCK='/run/ffn-fe100-tables.lock'
SPECS={'smac':8,'nexthop':16}


class ResourceTables:
    def __init__(self,pools,lock_fd=None):
        if set(pools)!=set(SPECS):raise ValueError('Explicit resource pools required')
        self.pools={k:list(v) for k,v in pools.items()}
        for k,values in self.pools.items():
            if (not 1<=len(values)<=4096 or len(set(values))!=len(values) or
                any(type(i) is not int or not 0<=i<(1024 if k=='smac' else 65536) for i in values)):
                raise ValueError('Invalid resource pool')
        if lock_fd is not None:
            if os.readlink('/proc/self/fd/'+str(lock_fd))!=LOCK:raise ValueError('Invalid table owner lock')
            self.lock=os.fdopen(os.dup(lock_fd),'a')
        else:self.lock=open(LOCK,'a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:self.lock.close();raise
        self.workers={};self.uncertain=set()

    def call(self,kind,operation,index,data=None):
        if kind not in self.pools or type(index) is not int or index not in self.pools[kind]:raise ValueError('Outside commissioned pool')
        if operation not in ('fetch','insert','delete'):raise ValueError('Invalid resource operation')
        if operation=='insert' and (not isinstance(data,bytes) or len(data)!=SPECS[kind]):raise ValueError('Invalid resource entry')
        if (kind,index) in self.uncertain and operation!='fetch':raise RuntimeError('Ambiguous resource operation requires readback')
        if kind in self.workers and self.workers[kind][0].poll() is not None:
            self.stop(kind)
            if operation!='fetch':
                self.uncertain.add((kind,index))
                raise RuntimeError('Resource worker exited; reconcile by readback')
        if kind not in self.workers:
            err=tempfile.TemporaryFile(mode='w+')
            try:
                p=subprocess.Popen([sys.executable,__file__,'--worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=err,bufsize=0,pass_fds=(self.lock.fileno(),),
                    env=dict(os.environ,FFN_FE100_LOCK_FD=str(self.lock.fileno())))
                self.workers[kind]=(p,err,Lines(p.stdout.fileno()))
                send(p.stdin.fileno(),dict(kind=kind,pool=self.pools[kind]))
            except BaseException:
                if kind in self.workers:self.stop(kind)
                else:err.close()
                raise
        p,err,lines=self.workers[kind]
        try:
            send(p.stdin.fileno(),dict(op=operation,index=index,data=data.hex() if data is not None else None),12)
            result=lines.read(12)
            if set(result)!={'rc','data'} or type(result['rc']) is not int:raise RuntimeError('Invalid resource response')
            if result['rc'] not in ((0,3) if operation=='fetch' else (0,)):raise RuntimeError('Native resource '+operation+' failed: '+str(result['rc']))
            raw=bytes.fromhex(result['data'])
            if len(raw)!=SPECS[kind]:raise RuntimeError('Invalid resource readback length')
            if operation=='fetch':self.uncertain.discard((kind,index))
            return None if result['rc']==3 else raw
        except BaseException:
            self.uncertain.add((kind,index))
            self.stop(kind);raise

    def fetch(self,kind,index):return self.call(kind,'fetch',index)
    def insert(self,kind,index,data):self.call(kind,'insert',index,data)
    def delete(self,kind,index):self.call(kind,'delete',index)
    def stop(self,kind):
        p,err,_=self.workers.pop(kind)
        try:p.stdin.close()
        except BrokenPipeError:pass
        try:p.wait(timeout=2)
        except subprocess.TimeoutExpired:p.kill();p.wait()
        p.stdout.close();err.close()
    def close(self):
        for kind in list(self.workers):self.stop(kind)
        self.lock.close()


def worker():
    fd=int(os.environ['FFN_FE100_LOCK_FD'])
    if os.readlink('/proc/self/fd/'+str(fd))!=LOCK:raise ValueError('Owner table lock required')
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    lines=Lines(0);config=lines.read();kind=config['kind'];pool=config['pool']
    if (set(config)!={'kind','pool'} or kind not in SPECS or not isinstance(pool,list) or
        not 1<=len(pool)<=4096 or len(set(pool))!=len(pool) or
        any(type(i) is not int or not 0<=i<(1024 if kind=='smac' else 65536) for i in pool)):
        raise ValueError('Invalid worker scope')
    if sys.byteorder!='big' or C.sizeof(C.c_void_p)!=8:raise RuntimeError('MIPS64 big-endian ABI required')
    from ffn_fe100 import bar0_base_and_size,memory_decode_on,register_map_path
    base,size=bar0_base_and_size()
    if size!=0x100000 or not memory_decode_on():raise RuntimeError('FE100 BAR unavailable')
    native=C.CDLL('/usr/local/lib/libffn-fe100-resources.so',mode=os.RTLD_LOCAL|os.RTLD_NOW)
    native.ffn_fe100_resources_abi.restype=C.c_uint
    if native.ffn_fe100_resources_abi()!=1:raise RuntimeError('Resource driver ABI changed')
    native.ffn_fe100_resources_open.argtypes=[C.c_int,C.c_int,C.c_uint64,C.c_uint,
        C.POINTER(C.c_uint32),C.c_size_t,C.POINTER(C.c_uint32),C.c_size_t,C.c_char_p]
    native.ffn_fe100_resources_open.restype=C.c_int
    native.ffn_fe100_resources_call.argtypes=[C.c_uint,C.c_uint32,C.c_void_p,C.c_size_t]
    native.ffn_fe100_resources_call.restype=C.c_int
    registers=[r['addr'] for r in json.loads(Path(register_map_path()).read_text())]
    if any(type(r) is not int or r<0 or r>=0x100000 or r%4 for r in registers):
        raise ValueError('Invalid resource register map')
    native_pool=(C.c_uint32*len(pool))(*pool)
    native_registers=(C.c_uint32*len(registers))(*registers)
    trace=('/var/lib/ffn/fe100/resource-'+kind+'-'+str(time.time_ns())+'.txt').encode()
    # The native driver dlopens this exact hashed descriptor. A concurrent
    # library replacement cannot substitute a different owner ABI by path.
    with open(LIB,'rb') as owner:
        if hashlib.sha256(owner.read()).hexdigest()!=SHA:raise RuntimeError('Owner ABI changed')
        rc=native.ffn_fe100_resources_open(fd,owner.fileno(),base,
            1 if kind=='smac' else 2,native_pool,len(pool),native_registers,len(registers),trace)
    if rc:raise RuntimeError('Native resource initialization failed: '+str(rc))
    while True:
        try:request=lines.read(30)
        except (EOFError,TimeoutError):return
        op=request['op'];index=request['index'];data=request['data']
        if set(request)!={'op','index','data'} or op not in ('fetch','insert','delete') or type(index) is not int or index not in pool:
            raise ValueError('Invalid scoped operation')
        raw=bytes.fromhex(data) if op=='insert' else bytes(SPECS[kind])
        if len(raw)!=SPECS[kind] or op!='insert' and data is not None:raise ValueError('Invalid entry data')
        entry=(C.c_ubyte*SPECS[kind]).from_buffer_copy(raw)
        rc=native.ffn_fe100_resources_call({'fetch':1,'insert':2,'delete':3}[op],index,entry,len(raw))
        if rc<0:raise RuntimeError('Native resource operation failed: '+str(rc))
        send(1,dict(rc=rc,data=bytes(entry).hex()),10)


if __name__=='__main__':
    if sys.argv[1:]!=['--worker']:raise SystemExit('Internal owner worker only')
    worker()
