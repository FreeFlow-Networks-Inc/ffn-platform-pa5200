import json
import socketserver
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch
import ffn_dp_agent as agent


class AgentTests(unittest.TestCase):
    def test_policy_ack_is_observed_and_unavailable_is_not_ready(self):
        value={'available':True,'applied':False,'revision':2,'processing':{'acknowledged':False,'hardware_offload':False},'nat':{'acknowledged':False},'private':'omit'}
        with patch.dict('sys.modules',{'ffn_security_runtime':SimpleNamespace(status=lambda:value)}):
            self.assertEqual(agent.policy_status(),{k:v for k,v in value.items() if k!='private'})
        def failed():raise OSError('unavailable')
        with patch.dict('sys.modules',{'ffn_security_runtime':SimpleNamespace(status=failed)}):
            self.assertFalse(agent.policy_status()['applied'])

    def test_live_nonce_exchange(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'agent.sock')
            with socketserver.UnixStreamServer(path, agent.Handler) as server:
                thread = threading.Thread(target=server.handle_request)
                with patch.object(agent, 'snapshot', side_effect=lambda n: {'nonce':n, 'protocol':1, 'role':'dataplane', 'state':'ready'}):
                    thread.start()
                    self.assertEqual(agent.handshake(path)['state'], 'ready')
                    thread.join(8)
                    self.assertFalse(thread.is_alive())

    def test_replay_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/'agent.sock')
            with socketserver.UnixStreamServer(path, agent.Handler) as server:
                thread = threading.Thread(target=server.handle_request)
                with patch.object(agent, 'snapshot', return_value={'nonce':'old', 'protocol':1, 'role':'dataplane'}):
                    thread.start()
                    with self.assertRaises(ValueError): agent.handshake(path)
                    thread.join(8)


if __name__ == '__main__': unittest.main()
