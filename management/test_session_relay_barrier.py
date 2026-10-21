#!/usr/bin/env python3
"""The relay moves the CP's FE100 policy barrier to the committed configuration when it lags.

Pure functions with a scripted plane request; one case drives the real socket helper
against a local listener. Nothing here touches the appliance's units or files."""
import json
import os
import socket
import tempfile
import threading
import unittest

import session_relay

CONFIGURATION = {'config_digest': 'c' * 64}


def scripted(status, apply=None):
    calls = []
    def request(resource, action, payload, timeout):
        calls.append((resource, action, payload, timeout))
        if action == 'status': return status
        if action == 'apply': return apply
        raise AssertionError(action)
    return request, calls


class BarrierResync(unittest.TestCase):
    def test_matching_barrier_needs_no_request_beyond_status(self):
        request, calls = scripted({'ok': True, 'result': {'revision': 530, 'digest': 'c' * 64, 'sessions': 0}})
        self.assertEqual(session_relay.barrier_resync(CONFIGURATION, request, log=lambda _: None), 'matched')
        self.assertEqual([c[1] for c in calls], ['status'])

    def test_lagging_barrier_without_hardware_sessions_is_moved(self):
        logged = []
        request, calls = scripted({'ok': True, 'result': {'revision': 451, 'digest': 'a' * 64, 'sessions': 0, 'recovery_required': False}},
                                  apply={'ok': True, 'result': {'revision': 452}})
        self.assertEqual(session_relay.barrier_resync(CONFIGURATION, request, log=logged.append), 'resynced')
        self.assertEqual(calls[1][:3], ('fe100-policy', 'apply', {'revision': 451, 'digest': 'c' * 64}))
        self.assertGreaterEqual(calls[1][3], 120, 'the apply drains and re-recovers; it needs the long budget')
        self.assertIn('451 -> 452', logged[0])

    def test_hardware_sessions_defer_to_the_commit_hook(self):
        for state in ({'revision': 7, 'digest': 'a' * 64, 'sessions': 3},
                      {'revision': 7, 'digest': 'a' * 64, 'sessions': {'x': 1}},
                      {'revision': 7, 'digest': 'a' * 64, 'sessions': 0, 'recovery_required': True}):
            logged = []
            request, calls = scripted({'ok': True, 'result': state})
            self.assertEqual(session_relay.barrier_resync(CONFIGURATION, request, log=logged.append), 'deferred', state)
            self.assertEqual([c[1] for c in calls], ['status'], 'no apply while sessions or recovery depend on the barrier')
            self.assertIn('left to the commit hook', logged[0])

    def test_refused_apply_is_reported_not_raised(self):
        logged = []
        request, _ = scripted({'ok': True, 'result': {'revision': 7, 'digest': 'a' * 64, 'sessions': 0}},
                              apply={'ok': False, 'error': 'revision conflict'})
        self.assertEqual(session_relay.barrier_resync(CONFIGURATION, request, log=logged.append), 'deferred')
        self.assertIn('revision conflict', logged[0])

    def test_daemon_away_or_unusable_status_is_unavailable(self):
        for status in (None, {'ok': False, 'error': 'busy'}, {'ok': True, 'result': 'odd'}, {'ok': True, 'result': {}}):
            request, _ = scripted(status)
            self.assertEqual(session_relay.barrier_resync(CONFIGURATION, request, log=lambda _: None), 'unavailable', status)

    def test_plane_request_speaks_the_daemon_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'control.sock')
            seen = []
            server = socket.socket(socket.AF_UNIX); server.bind(path); server.listen(1)
            def serve():
                link, _ = server.accept()
                with link:
                    data = b''
                    while not data.endswith(b'\n'): data += link.recv(65536)
                    seen.append(json.loads(data))
                    link.sendall(json.dumps({'v': 1, 'id': seen[0]['id'], 'ok': True, 'result': {'revision': 9}}).encode() + b'\n')
            thread = threading.Thread(target=serve); thread.start()
            try:
                reply = session_relay.plane_request('fe100-policy', 'status', {}, 5, path=path)
            finally:
                thread.join(5); server.close()
            self.assertEqual(seen[0]['resource'], 'fe100-policy'); self.assertEqual(seen[0]['action'], 'status'); self.assertEqual(seen[0]['v'], 1)
            self.assertEqual(reply['result'], {'revision': 9})
            self.assertIsNone(session_relay.plane_request('fe100-policy', 'status', {}, 1, path=os.path.join(directory, 'absent.sock')))


if __name__ == '__main__':
    unittest.main()
