import copy
import math
import unittest
import uuid
from unittest.mock import Mock
import ffn_oct


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.state = {'owner': 'ffn-controld', 'worker_configured': True, 'agents': {}}
        self.roles = {}
        for role in ('cp', 'dp'):
            boot = str(uuid.uuid4())
            self.state['agents'][role] = {
                'role': role, 'connected': True, 'fresh': True, 'ready': True,
                'age_seconds': 2, 'stale_after_seconds': 40,
                'last_observation': {'v': 1, 'platform': 'pa5200', 'role': role,
                    'boot_id': boot, 'observed_at': 0,
                    'report': {'boot_id': boot, 'ready': True}}}
            self.roles[role] = {'fresh': True, 'ready': True, 'boot_id': boot}
        self.worker = {'v': 1, 'ok': True, 'state': 'observed', 'trace': ['controld', 'mp'],
                       'result': {'platform': 'pa5200', 'busy': False, 'roles': self.roles}}
        self.client = Mock()
        self.client.query.side_effect = self.query

    def query(self, path, **args):
        if path == 'state/agents':return self.state
        self.assertEqual(path, 'plane/request')
        request = args['request']
        self.assertEqual((request['resource'], request['action'], request['payload']),
                         ('plane-lifecycle', 'status', {}))
        return dict(self.worker, id=request['id'])

    def result(self, model='PA-5220'):
        return ffn_oct.agent_handoff(ffn_oct.PROFILES[model], self.client)

    def test_current_agents_and_worker_acknowledgement_ignore_processor_clock_skew(self):
        r = self.result()
        self.assertTrue(r['ready'])
        self.assertTrue(r['dp_handshake'])
        self.assertTrue(r['control_handoff'])
        self.assertEqual(self.client.query.call_count, 2)

    def test_missing_stale_disconnected_unready_or_wrong_identity_waits(self):
        original = copy.deepcopy(self.state)
        cases = [('fresh', False), ('connected', False), ('ready', False),
                 ('age_seconds', 40), ('age_seconds', -1), ('age_seconds', math.nan),
                 ('age_seconds', True), ('stale_after_seconds', math.inf),
                 ('stale_after_seconds', None), ('role', 'cp')]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.state = copy.deepcopy(original)
                self.state['agents']['dp'][key] = value
                self.assertFalse(self.result()['ready'])
        for key, value in [('platform', 'generic'), ('role', 'cp'), ('boot_id', str(uuid.uuid4())),
                           ('boot_id', 'bad'), ('v', 0)]:
            self.state = copy.deepcopy(original)
            self.state['agents']['dp']['last_observation'][key] = value
            self.assertFalse(self.result()['ready'])
        self.state = copy.deepcopy(original)
        del self.state['agents']['dp']
        self.assertFalse(self.result()['ready'])

    def test_worker_missing_failed_wrong_trace_busy_or_rebooting_does_not_handoff(self):
        self.state['worker_configured'] = False
        self.assertFalse(self.result()['control_handoff'])
        self.state['worker_configured'] = True
        original = copy.deepcopy(self.worker)
        for key, value in [('ok', False), ('state', 'unknown'), ('trace', ['mp']), ('v', 2)]:
            self.worker = dict(original, **{key: value})
            r = self.result()
            self.assertTrue(r['dp_handshake'])
            self.assertFalse(r['control_handoff'])
        self.worker = copy.deepcopy(original)
        self.worker['result']['busy'] = True
        self.assertFalse(self.result()['ready'])
        self.worker = copy.deepcopy(original)
        self.worker['result']['roles']['dp']['boot_id'] = str(uuid.uuid4())
        self.assertFalse(self.result()['ready'])

    def test_transport_error_is_waiting_not_an_exception(self):
        self.client.query.side_effect = RuntimeError('socket unavailable')
        self.assertFalse(self.result()['ready'])

    def test_all_expected_dataplanes_required_duplicates_do_not_count(self):
        self.assertFalse(self.result('PA-5250')['dp_handshake'])
        self.state['agents']['dp2'] = copy.deepcopy(self.state['agents']['dp'])
        self.assertFalse(self.result('PA-5250')['dp_handshake'])
        observed = self.state['agents']['dp2']['last_observation']
        observed['boot_id'] = observed['report']['boot_id'] = str(uuid.uuid4())
        self.assertTrue(self.result('PA-5250')['dp_handshake'])


if __name__ == '__main__':unittest.main()
