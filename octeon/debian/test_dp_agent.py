import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import ffn_dp_agent as agent


class AgentTests(unittest.TestCase):
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
