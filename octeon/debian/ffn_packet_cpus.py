"""Control-plane allocation of native packet worker CPUs across owners."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import uuid


DRIVER_STATUS=Path('/sys/kernel/debug/ffn_dp_packet_init/status')


def driver_cpus(status=DRIVER_STATUS):
    """The CPUs the trunk driver pinned its receive threads to (its status names them), or none."""
    try:
        cpus=json.loads(Path(status).read_text()).get('trunk',{}).get('rx_cpus',[])
        return {c for c in cpus if type(c) is int and c>=0}
    except (OSError,ValueError,AttributeError,TypeError):return set()


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

    def reserve(self,allowed=None,count=2,driver=None,pid=None):
        """`count` CPUs (two per lane), least used first, never the trunk driver's receive cores
        while enough others remain. `pid` names the owner the record is bound to (a native
        owner reserving through the command line); the reservation lives as long as it."""
        if pid is None:pid=os.getpid()
        if type(pid) is not int or pid<=0:raise ValueError('Invalid reservation owner')
        allowed=sorted(set(os.sched_getaffinity(0) if allowed is None else allowed))
        if not allowed or any(type(c) is not int or c<0 for c in allowed):
            raise ValueError('No valid packet worker CPU affinity')
        if type(count) is not int or not 1<=count<=16:raise ValueError('Invalid worker CPU count')
        # Preserve a control/driver CPU when the allowed mask has spare cores.
        candidates=allowed[1:] if len(allowed)>2 else allowed
        driver=driver_cpus() if driver is None else set(driver)
        spared=[c for c in candidates if c not in driver]
        if len(spared)>=count:candidates=spared
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
            for _ in range(count):
                cpu=min(candidates,key=lambda c:(usage[c],c))
                cpus.append(cpu);usage[cpu]+=1
            token=uuid.uuid4().hex
            record=dict(self.identity(pid),cpus=cpus)
            temporary=self.root/(token+'.tmp')
            temporary.write_text(json.dumps(record));temporary.replace(self.root/(token+'.json'))
            return token,tuple(cpus)

    def release(self,token):
        if not isinstance(token,str) or uuid.UUID(hex=token).hex!=token:
            raise ValueError('Invalid CPU reservation token')
        with self.locked():(self.root/(token+'.json')).unlink(missing_ok=True)


def main(argv=None):
    """`reserve --count N --pid P` prints {"token", "cpus"}; `release TOKEN` drops a record."""
    import argparse
    parser=argparse.ArgumentParser(description='Reserve or release native packet worker CPUs.')
    verbs=parser.add_subparsers(dest='verb',required=True)
    reserve=verbs.add_parser('reserve');reserve.add_argument('--count',type=int,default=2);reserve.add_argument('--pid',type=int,default=os.getpid())
    release=verbs.add_parser('release');release.add_argument('token')
    args=parser.parse_args(argv)
    reservations=CpuReservations()
    if args.verb=='reserve':
        token,cpus=reservations.reserve(count=args.count,pid=args.pid)
        print(json.dumps(dict(token=token,cpus=list(cpus))))
    else:reservations.release(args.token)
    return 0


if __name__=='__main__':raise SystemExit(main())
