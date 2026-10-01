#!/usr/bin/env python3
"""Independent CP owner supervisor. Control/process I/O only; never admits flows.

The trusted embedding service supplies its owner command and bounded recovery
callback. The callback must acknowledge ingress, flow and resource withdrawal
in that order. Native device operations stay in the existing C adapters.
An inherited private socket carries progress, not a public admission API.
Run the supervisor as a restarting service; its durable report identifies an
orphaned child cgroup after supervisor death. No PID-based process adoption.
"""
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

TOKEN=struct.Struct('!QQ16s')
CGROUP=Path('/sys/fs/cgroup')
PREFIX='ffn-fe100-owner-'


class Lease:
    """Owner pulses only after successful dependency checks or bounded progress."""
    def __init__(self):
        self.nonce=uuid.UUID(os.environ['FFN_FE100_GUARD_NONCE']).bytes
        self.sock=socket.socket(fileno=int(os.environ['FFN_FE100_GUARD_FD']))
        self.sock.setblocking(False)
        self.sequence=0

    def pulse(self):
        self.sequence+=1
        raw=TOKEN.pack(self.sequence,time.monotonic_ns(),self.nonce)
        if self.sock.send(raw)!=len(raw):raise RuntimeError('guard heartbeat was not delivered')


class Withdrawal:
    def __init__(self,ingress,sessions,resources):
        self.steps=(('ingress',ingress),('sessions',sessions),('resources',resources))

    def __call__(self,reason):
        for name,run in self.steps:
            if run(reason) is not True:raise RuntimeError(name+' withdrawal not acknowledged')
        return True


def publish(path,state):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    try:
        with tmp.open('w') as out:
            os.chmod(tmp,0o600);json.dump(state,out);out.flush();os.fsync(out.fileno())
        tmp.replace(path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:tmp.unlink(missing_ok=True)


def group_path(nonce):
    if str(uuid.UUID(nonce))!=nonce:raise ValueError('invalid guardian identity')
    return CGROUP/(PREFIX+uuid.UUID(nonce).hex)


def stop_group(group,timeout=5):
    """Kill descendants, including ones which escaped the process group."""
    if not group.exists():return
    if group.parent!=CGROUP or not group.name.startswith(PREFIX):raise ValueError('not an owned cgroup')
    (group/'cgroup.kill').write_text('1')
    deadline=time.monotonic()+timeout
    while True:
        events=dict(line.split() for line in (group/'cgroup.events').read_text().splitlines())
        if events.get('populated')=='0':return
        if time.monotonic()>=deadline:raise RuntimeError('owner cgroup is still populated')
        time.sleep(.02)


def supervise(*args,**kwargs):
    def terminate(signum,frame):raise InterruptedError('guardian termination requested')
    previous=signal.signal(signal.SIGTERM,terminate)
    try:return _supervise(*args,**kwargs)
    finally:signal.signal(signal.SIGTERM,previous)


def _supervise(command,recover,report,*,timeout=3,startup_timeout=15,env=None,
              stdin=None,stdout=None,stderr=None):
    """Launch a trusted owner after recovery; stop it before every final drain.

    Callbacks must be bounded, generation-scoped and read back native state.
    A False/failed callback never acknowledges withdrawal or restarts an owner.
    This function does not return until child cleanup and recovery are checked.
    """
    for value in (timeout,startup_timeout):
        if type(value) not in (int,float) or not math.isfinite(value) or not .1<=value<=60:
            raise ValueError('bounded guardian timeout required')
    if not callable(recover) or not isinstance(command,list) or not command or any(
            not isinstance(v,str) or not v or '\0' in v for v in command):
        raise ValueError('trusted owner command and recovery callback required')
    report=Path(report);report.parent.mkdir(parents=True,exist_ok=True)
    with report.with_suffix(report.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        state=dict(schema=1,cp_boot_id=boot,phase='recovering',admission_enabled=False)
        if report.exists():
            previous=json.loads(report.read_text())
            if previous.get('schema')!=1:raise ValueError('unknown guardian journal')
            if previous.get('group_nonce'):
                old=group_path(previous['group_nonce'])
                # A prior supervisor may have died while its owner remained
                # active. Fence that entire cgroup before opening any journal.
                stop_group(old)
                if old.exists():old.rmdir()
        publish(report,state)
        try:
            if recover('startup recovery') is not True:raise RuntimeError('startup drain not acknowledged')
        except BaseException as error:
            state.update(phase='blocked',error=str(error));publish(report,state);raise
        nonce=str(uuid.uuid4());group=group_path(nonce)
        group.mkdir()
        child=None;pidfd=None
        parent,channel=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        reason='owner startup failed';error=None
        try:
            if not (group/'cgroup.kill').exists():raise RuntimeError('cgroup.kill unavailable')
            state.update(phase='starting',group_nonce=nonce);publish(report,state)
            child_env=dict(os.environ if env is None else env,
                FFN_FE100_GUARD_FD=str(channel.fileno()),FFN_FE100_GUARD_NONCE=nonce)
            child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--owned',nonce,*command],
                pass_fds=(channel.fileno(),),env=child_env,start_new_session=True,
                stdin=stdin,stdout=stdout,stderr=stderr)
            channel.close();pidfd=os.pidfd_open(child.pid)
            state['owner_pid']=child.pid;publish(report,state)
            sequence=0;sent=0;deadline=time.monotonic()+startup_timeout
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:reason='owner progress lease expired';break
                ready=select.select([parent,pidfd],[],[],remaining)[0]
                # Keep the child unreaped until all descendants have been
                # killed; pidfd observes exit without trusting a reusable PID.
                if pidfd in ready:reason='owner exited';break
                if parent not in ready:continue
                raw,_,flags,_=parent.recvmsg(TOKEN.size)
                now=time.monotonic();now_ns=time.monotonic_ns()
                if now>=deadline:reason='owner progress lease expired';break
                if len(raw)!=TOKEN.size or flags&socket.MSG_TRUNC:
                    reason='owner heartbeat closed or malformed';break
                seq,stamp,identity=TOKEN.unpack(raw)
                if (seq!=sequence+1 or identity!=uuid.UUID(nonce).bytes or stamp<sent or
                        stamp>now_ns or now_ns-stamp>=int(timeout*1e9)):
                    reason='owner heartbeat lost continuity or freshness';break
                sequence,sent=seq,stamp;deadline=stamp/1e9+timeout
                if state['phase']!='running':
                    state.update(phase='running');publish(report,state)
        except BaseException as exc:
            error=exc;reason='supervisor failure: '+str(exc)
        finally:
            channel.close();parent.close()
            try:
                # Also kill the bootstrap if it has not yet joined the group.
                # Its only pre-join action is cgroup entry, never device I/O.
                if pidfd is not None:
                    try:signal.pidfd_send_signal(pidfd,signal.SIGKILL)
                    except ProcessLookupError:pass
                elif child is not None:child.kill()
                stop_group(group)
                if child is not None:state['owner_exit_code']=child.wait(timeout=5)
                state.update(phase='draining',reason=reason);publish(report,state)
                if recover(reason) is not True:raise RuntimeError('final drain not acknowledged')
                group.rmdir()
                state.update(phase='drained',group_nonce=None,withdrawal_acknowledged=True)
            except BaseException as exc:
                state.update(phase='blocked',reason=reason,error=str(exc),withdrawal_acknowledged=False)
                error=exc
            finally:
                if pidfd is not None:os.close(pidfd)
                if child is not None:
                    for pipe in (child.stdin,child.stdout,child.stderr):
                        if pipe is not None:pipe.close()
                publish(report,state)
        if error is not None:raise error
        return state


def owned():
    # Executed in the supervised child. No arbitrary cgroup path is accepted.
    if len(sys.argv)<4 or sys.argv[1]!='--owned':raise SystemExit('internal owner bootstrap only')
    nonce=sys.argv[2]
    if os.environ.get('FFN_FE100_GUARD_NONCE')!=nonce:raise RuntimeError('guardian identity mismatch')
    group=group_path(nonce)
    (group/'cgroup.procs').write_text(str(os.getpid()))
    os.execvpe(sys.argv[3],sys.argv[3:],os.environ)


if __name__=='__main__':owned()
