"""Explicit privileged cgroup tests, with no device or packet I/O.

FFN_FE100_GUARD_PROCESS_TEST=yes python3 -m unittest test_guard_process
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from ffn_fe100_guard import supervise,group_path,stop_group

FIXTURE=r'''
import os,signal,time,sys,fcntl
from pathlib import Path
from ffn_fe100_guard import Lease,TOKEN
lease=Lease();lease.pulse()
mode=sys.argv[1]
if mode=='duplicate':lease.sequence=0;lease.pulse()
if mode=='malformed':lease.sock.send(b'x')
if mode=='stall':os.kill(os.getpid(),signal.SIGSTOP)
if mode=='descendant':
 child=os.fork()
 if child==0:
  os.setsid()
  with open(sys.argv[2],'a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX);Path(sys.argv[2]+'.ready').touch()
   while True:time.sleep(.05)
 while not Path(sys.argv[2]+'.ready').exists():time.sleep(.01)
 Path(sys.argv[2]+'.pid').write_text(str(child))
 os._exit(9)
if mode=='pulse':
 while True:lease.pulse();time.sleep(.05)
if mode=='wait':time.sleep(10)
'''


@unittest.skipUnless(os.environ.get('FFN_FE100_GUARD_PROCESS_TEST')=='yes','explicit privileged cgroup test')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.report=self.root/'guard.json'
        self.env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent))
        self.calls=[]

    def recover(self,reason):self.calls.append(reason);return True

    def run_owner(self,mode,recover=None):
        return supervise([sys.executable,'-c',FIXTURE,mode,str(self.root/'child.lock')],recover or self.recover,
            self.report,timeout=.3,startup_timeout=3,env=self.env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)

    def test_exit_stall_and_protocol_failure_all_drain(self):
        for mode in ('exit','stall','wait','malformed','duplicate'):
            with self.subTest(mode=mode):
                state=self.run_owner(mode)
                self.assertEqual(state['phase'],'drained')
                self.assertFalse(state['admission_enabled'])
                self.assertTrue(state['withdrawal_acknowledged'])
                if mode in ('stall','wait'):self.assertIn('expired',state['reason'])
                if mode=='exit':
                    self.assertIn('owner_pid',state)
                    # Closing the heartbeat can precede interpreter teardown;
                    # the guardian is allowed to kill that final cleanup too.
                    self.assertIn(state['owner_exit_code'],(0,-signal.SIGKILL))
        self.assertEqual(len(self.calls),10)

    def test_setsid_descendant_is_dead_before_recovery(self):
        def recover(reason):
            if reason!='startup recovery':
                with (self.root/'child.lock').open('a') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return True
        state=self.run_owner('descendant',recover)
        self.assertEqual(state['phase'],'drained')
        self.assertTrue((self.root/'child.lock.ready').exists())
        self.assertEqual(state['owner_exit_code'],9)

    def test_failed_recovery_stays_blocked(self):
        def recover(reason):return reason=='startup recovery'
        with self.assertRaisesRegex(RuntimeError,'drain'):self.run_owner('exit',recover)
        state=json.loads(self.report.read_text());self.assertEqual(state['phase'],'blocked')
        self.assertFalse(state['withdrawal_acknowledged'])
        group=group_path(state['group_nonce']);stop_group(group);group.rmdir()

    def test_supervisor_crash_recovery_stops_orphan_before_launch(self):
        script='''from ffn_fe100_guard import supervise
import sys,os
supervise([sys.executable,'-c',FIXTURE,'pulse'],lambda _:True,sys.argv[1],timeout=.3,env=dict(os.environ))
'''.replace('FIXTURE',repr(FIXTURE))
        parent=subprocess.Popen([sys.executable,'-c',script,str(self.report)],env=self.env,
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        old=None
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if self.report.exists():
                    state=json.loads(self.report.read_text())
                    if state.get('phase')=='running':old=group_path(state['group_nonce']);break
                time.sleep(.01)
            self.assertIsNotNone(old)
            parent.kill();parent.wait(timeout=3)
            self.assertIn('populated 1',(old/'cgroup.events').read_text())
            state=self.run_owner('exit')
            self.assertEqual(state['phase'],'drained');self.assertFalse(old.exists())
        finally:
            if parent.poll() is None:parent.kill();parent.wait(timeout=3)
            parent.stderr.close()
            if old is not None and old.exists():stop_group(old);old.rmdir()


if __name__=='__main__':unittest.main(verbosity=2)
