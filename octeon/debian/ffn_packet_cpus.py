"""Control-plane allocation of native packet worker CPUs across owners."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import uuid


def process_start(pid, proc=Path('/proc')):
    fields=(proc/str(pid)/'stat').read_text().rsplit(') ',1)[1].split()
    if fields[0]=='Z':raise ProcessLookupError(pid)
    return fields[19]


class CpuReservations:
    def __init__(self,root=Path('/run/ffn-packet-cpus'),run=Path('/run'),proc=Path('/proc')):
        self.root=Path(root);self.run=Path(run);self.proc=Path(proc)

    @contextmanager
    def locked(self):
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        with (self.root/'lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            yield

    def identity(self,pid):
        return dict(pid=pid,process_start=process_start(pid,self.proc),
                    boot_id=(self.proc/'sys/kernel/random/boot_id').read_text().strip())

    def live(self,row):
        try:
            return type(row['pid']) is int and row['pid']>0 and all(
                row.get(k)==v for k,v in self.identity(row['pid']).items())
        except (OSError,ValueError,KeyError,IndexError,TypeError):return False

    def reserve(self,allowed=None):
        allowed=sorted(set(os.sched_getaffinity(0) if allowed is None else allowed))
        if not allowed or any(type(c) is not int or c<0 for c in allowed):
            raise ValueError('No valid packet worker CPU affinity')
        # Preserve a control/driver CPU when the allowed mask has spare cores.
        candidates=allowed[1:] if len(allowed)>2 else allowed
        with self.locked():
            usage={cpu:0 for cpu in candidates};claimed=set()
            for path in self.root.glob('*.json'):
                row=json.loads(path.read_text())
                if not self.live(row):path.unlink();continue
                claimed.add(row['pid'])
                for cpu in row['cpus']:
                    if cpu in usage:usage[cpu]+=1
            # During rolling upgrades, respect live owners without reservations.
            paths=[self.run/'ffn-fabric.json',*self.run.glob('ffn-physical-*-status.json'),
                   *self.run.glob('ffn-aggregate-ae*-status.json')]
            observed=set()
            for path in paths:
                try:
                    row=json.loads(path.read_text())
                    if row.get('pid') in claimed or not self.live(row):continue
                    workers=row.get('workers') or {}
                    for direction in ('rx','tx'):
                        tid=workers[direction+'_tid']
                        if type(tid) is not int or tid in observed:continue
                        # Require this TID to belong to the verified process.
                        if not (self.proc/str(row['pid'])/'task'/str(tid)).is_dir():continue
                        observed.add(tid)
                        affinity=os.sched_getaffinity(tid)
                        if len(affinity)==1:
                            cpu=next(iter(affinity))
                            if cpu in usage:usage[cpu]+=1
                except (OSError,ValueError,KeyError,IndexError,TypeError):continue
            cpus=[]
            for _ in range(2):
                cpu=min(candidates,key=lambda c:(usage[c],c))
                cpus.append(cpu);usage[cpu]+=1
            token=uuid.uuid4().hex
            record=dict(self.identity(os.getpid()),cpus=cpus)
            temporary=self.root/(token+'.tmp')
            temporary.write_text(json.dumps(record));temporary.replace(self.root/(token+'.json'))
            return token,tuple(cpus)

    def release(self,token):
        if not isinstance(token,str) or uuid.UUID(hex=token).hex!=token:
            raise ValueError('Invalid CPU reservation token')
        with self.locked():(self.root/(token+'.json')).unlink(missing_ok=True)
