"""Opt-in root-only Unix RPC tests; no hardware access."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ffn_fe100_control_socket import request,send,receive

FIXTURE='''import sys,os
from pathlib import Path
import ffn_fe100_policy_control as policy
import ffn_fe100_controld as service
policy.ROOT=Path(sys.argv[1])
class Lease:
 def pulse(self):pass
service.Lease=Lease
service.serve(Path(sys.argv[2]))
'''


@unittest.skipUnless(os.environ.get('FFN_FE100_CONTROL_PROCESS_TEST')=='yes','explicit privileged RPC test')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'control.sock';self.child=None
        self.addCleanup(self.stop);self.start()

    def start(self):
        self.nonce=str(uuid.uuid4())
        self.child=subprocess.Popen([sys.executable,'-c',FIXTURE,str(self.root),str(self.path)],
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent)+os.pathsep+os.environ.get('PYTHONPATH',''),
                     FFN_FE100_GUARD_NONCE=self.nonce),
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                state=request('status',{},self.path,timeout=.2)
                if state['control_owner']==self.nonce:return
            except (OSError,RuntimeError):pass
            if self.child.poll() is not None:raise RuntimeError(self.child.stderr.read().decode())
            time.sleep(.02)
        raise RuntimeError('server did not start')

    def stop(self):
        if self.child is not None:
            if self.child.poll() is None:self.child.kill();self.child.wait(timeout=3)
            self.child.stderr.close();self.child=None

    def test_revision_owner_identity_and_restart(self):
        state=request('replace',dict(revision=0,digest='a'*64),self.path)
        self.assertEqual(state['revision'],1);self.assertEqual(state['control_owner'],self.nonce)
        with self.assertRaisesRegex(RuntimeError,'revision conflict'):
            request('replace',dict(revision=0,digest='a'*64),self.path)
        previous=self.nonce;self.stop();self.start()
        state=request('status',{},self.path)
        self.assertEqual(state['revision'],1);self.assertNotEqual(state['control_owner'],previous)
        self.assertFalse(state['admission_enabled'])

    def test_supervised_observation_restarts_and_commit_invalidate_feed(self):
        from ffn_fe100_observations import ObservationClient
        from test_observations import NONCE,ROW,POLICY,message
        client=ObservationClient(NONCE,lambda op,payload:request(op,payload,self.path))
        for m in (message(1,'begin',POLICY),message(2,'snapshot',[ROW|{'padding':'x'*100000}]),message(3,'synchronized',{})):
            client.send(m)
        state=request('status',{},self.path)
        self.assertTrue(state['observations']['ready']);self.assertEqual(state['observations']['sessions'],1)
        self.assertFalse(state['admission_enabled'])
        state=request('replace',dict(revision=0,digest='a'*64),self.path)
        self.assertFalse(state['observations']['ready'])
        with self.assertRaises(RuntimeError):client.send(message(4,'heartbeat',{}))
        self.stop();self.start()
        self.assertFalse(request('status',{},self.path)['observations']['ready'])
        with self.assertRaisesRegex(RuntimeError,'owner changed'):client.close()

    def test_owner_timer_fences_silent_feed_without_client_request(self):
        from ffn_fe100_observations import ObservationClient
        from test_observations import NONCE,POLICY,message
        client=ObservationClient(NONCE,lambda op,payload:request(op,payload,self.path))
        client.send(message(1,'begin',POLICY));client.send(message(2,'synchronized',{}))
        time.sleep(11)
        state=request('status',{},self.path)
        self.assertFalse(state['observations']['ready'])
        self.assertIn('expired',state['observations']['reason'])
        self.assertFalse(state['admission_enabled'])

    def test_malformed_oversized_and_silent_clients_leave_owner_usable(self):
        for raw in (b'{',b'x'*70000):
            with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as sock:
                sock.settimeout(2);sock.connect(str(self.path));sock.send(raw)
                self.assertFalse(receive(sock)['ok'])
        with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as sock:
            sock.connect(str(self.path));time.sleep(.6)
        self.assertEqual(request('status',{},self.path)['control_owner'],self.nonce)

    def test_lost_response_does_not_repeat_replacement(self):
        with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as sock:
            sock.connect(str(self.path))
            send(sock,dict(schema=1,id=str(uuid.uuid4()),operation='replace',payload=dict(revision=0,digest='b'*64)))
        state=request('status',{},self.path)
        self.assertEqual(state['revision'],1)
        with self.assertRaisesRegex(RuntimeError,'revision conflict'):
            request('replace',dict(revision=0,digest='b'*64),self.path)

    def test_loose_socket_permissions_and_nonroot_peer_rejected(self):
        self.path.chmod(0o666)
        with self.assertRaises(PermissionError):request('status',{},self.path)
        self.path.chmod(0o600)
        # Kernel credential inspection rejects a peer before parsing commands.
        # Socketpair avoids needing to weaken the installed socket for a test.
        from ffn_fe100_control_socket import root_peer
        from unittest.mock import Mock
        import struct
        peer=Mock();peer.getsockopt.return_value=struct.pack('3i',1,65534,65534)
        with self.assertRaises(PermissionError):root_peer(peer)


if __name__=='__main__':unittest.main()
