import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import plane_lifecycle as p


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.before = {r: {'boot_id': ('a' if r == 'cp' else 'b') * 32, 'fresh': True, 'ready': True} for r in p.ROLES}
        self.client = p.Lifecycle(Path(self.tmp.name), observe=Mock(return_value=copy.deepcopy(self.before)))
        self.client.settings = Mock(return_value={r: {'enabled': True, 'timeout': 30} for r in p.ROLES})
        self.unit = {'LoadState':'loaded', 'ActiveState':'inactive', 'Type':'oneshot', 'RemainAfterExit':'no', 'Result':'success'}
        self.client.unit = Mock(return_value=self.unit)
        self.client.launch = Mock(); self.client.start_owner = Mock()

    def payload(self, role='dp'):
        return {'role':role,'operation':'restart','revision':0,'expected_boot_id':self.before[role]['boot_id'],'acknowledge_outage':True}

    def test_uncommissioned_and_missing_service_disabled(self):
        self.client.settings.return_value = {}
        self.assertFalse(self.client.status()['roles']['cp']['restart_available'])
        with self.assertRaises(ValueError): self.client.submit(self.payload())
        self.client.launch.assert_not_called()
        self.client.settings.return_value = {'cp':{'enabled':True,'timeout':30}}
        self.unit['LoadState'] = 'not-found'
        self.assertIn('oneshot', self.client.status()['roles']['cp']['reason'])

    def test_stale_and_wrong_boot_and_unacknowledged_rejected(self):
        for change in ({'expected_boot_id':'wrong'}, {'acknowledge_outage':False}, {'acknowledge_outage':1},
                       {'revision':True}, {'revision':9}, {'role':'mp'}, {'unit':'attacker.service'}):
            with self.assertRaises(ValueError): self.client.submit(dict(self.payload(), **change))
        self.before['dp']['fresh'] = False; self.client.observe.return_value = self.before
        with self.assertRaises(ValueError): self.client.submit(self.payload())
        self.client.launch.assert_not_called()

    def test_dp_requires_ready_cp(self):
        self.client.observe.return_value['cp']['ready'] = False
        with self.assertRaisesRegex(ValueError, 'Control Plane'): self.client.submit(self.payload())

    def test_one_role_and_duplicate_worker_never_resets_twice(self):
        job = self.client.submit(self.payload())
        new = copy.deepcopy(self.before); new['dp']['boot_id'] = 'c'*32
        self.client.observe.side_effect = [self.before, new]
        result = self.client.work(job['id'])
        self.assertEqual(result['status'], 'succeeded')
        self.client.start_owner.assert_called_once_with('dp',30)
        with self.assertRaises(ValueError): self.client.work(job['id'])
        self.assertEqual(self.client.start_owner.call_count,1)

    def test_serializes_cp_and_dp(self):
        self.client.submit(self.payload())
        with self.assertRaises(ValueError): self.client.submit(dict(self.payload('cp'),revision=1))
        self.assertTrue(self.client.status()['busy'])

    def test_new_boot_without_readiness_is_not_success(self):
        job = self.client.submit(self.payload())
        new = copy.deepcopy(self.before); new['dp'].update(boot_id='c'*32,ready=False)
        self.client.observe.side_effect = [self.before, new]
        result = self.client.work(job['id'],clock=Mock(side_effect=[0,1,31]),sleep=Mock())
        self.assertEqual(result['status'],'failed')
        self.assertIn('not verified',result['message'])

    def test_old_boot_never_counts_as_restarted(self):
        job = self.client.submit(self.payload())
        result = self.client.work(job['id'],clock=Mock(side_effect=[0,1,31]),sleep=Mock())
        self.assertEqual(result['status'],'failed')

    def test_other_processor_reset_is_reported(self):
        job = self.client.submit(self.payload())
        new = copy.deepcopy(self.before)
        for r in p.ROLES:new[r]['boot_id']='c'*32
        self.client.observe.side_effect=[self.before,new]
        self.assertIn('other processor',self.client.work(job['id'])['message'])

    def test_boot_changes_before_dispatch_cancels(self):
        job = self.client.submit(self.payload())
        self.client.observe.return_value['dp']['boot_id']='c'*32
        self.assertEqual(self.client.work(job['id'])['status'],'failed')
        self.client.start_owner.assert_not_called()

    def test_owner_failure_never_claims_success(self):
        job = self.client.submit(self.payload())
        self.client.start_owner.side_effect=RuntimeError('owner failed')
        self.assertEqual(self.client.work(job['id'])['status'],'failed')

    def test_missing_commissioning_is_readonly(self):
        client=p.Lifecycle(self.client.root,config=self.client.root/'missing.json')
        self.assertEqual(client.settings(),{})
        self.assertFalse(client.config.exists())


if __name__ == '__main__': unittest.main()
