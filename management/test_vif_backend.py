import unittest
from vif_backend import execute


class Backend(unittest.TestCase):
    def test_recovery_stops_dp_before_clearing_pending_cp_state(self):
        calls=[]
        def remote(op,data):calls.append(op);return {'config':{'revision':4,'vifs':{}}}
        def forward(op):
            calls.append('copper_'+op)
            return {'epoch':'current','state':{'epoch':'current','pending':'start'}}
        execute('apply',{'operation':'recover','revision':4},remote,
                lambda data:calls.append('drain'),forward=forward)
        self.assertEqual(calls,['status','drain','recover','copper_status','copper_recover'])

    def test_dp_failure_does_not_disable_under_a_running_owner(self):
        calls=[]
        def remote(op,data):
            calls.append(op)
            if op=='stop':raise RuntimeError('DP stop failed')
            return {'config':{'revision':4,'vifs':{}}}
        with self.assertRaises(RuntimeError):
            execute('apply',{'operation':'stop','revision':4},remote,
                    lambda data:None,forward=lambda op:calls.append('copper_'+op))
        self.assertEqual(calls,['status','stop'])

    def test_copper_start_is_after_policy_drain_before_dp_start(self):
        calls=[]
        def remote(op,data):
            calls.append(op)
            return {'config':{'revision':4,'vifs':{'fv3':{'port':3,'enabled':True}}}}
        def forward(op):calls.append('copper_'+op)
        execute('apply',{'operation':'start','revision':4},remote,
                lambda data:calls.append('drain'),forward=forward)
        self.assertEqual(calls,['status','drain','copper_start','start'])

    def test_physical_observation_uses_dp_challenge(self):
        calls=[]
        def remote(op,data):
            calls.append((op,data))
            return {'running':True,'link_token':'fresh','config':{'revision':4}}
        rows=[{'port':2}]
        execute('status',{},remote,read_links=lambda:rows)
        self.assertEqual(calls,[('status',{}),('links',{'token':'fresh','ports':rows})])

    def test_observation_failure_reports_error_without_configuration_write(self):
        calls=[]
        def remote(op,data):calls.append(op);return {'running':True,'link_token':'fresh'}
        def failed():raise RuntimeError('CP unavailable')
        result=execute('status',{},remote,read_links=failed)
        self.assertEqual(calls,['status','status'])
        self.assertIn('CP unavailable',result['link_observation_error'])

    def test_validate_then_drain_then_apply(self):
        calls=[]
        def remote(op,data):calls.append(op);return {'config':{'revision':4},'running':False}
        def drain(data):calls.append('drain');return {'drained':True}
        payload={'operation':'set','revision':4,'vifs':{}}
        execute('validate',payload,remote,drain);self.assertEqual(calls,['status','check'])
        calls.clear();execute('apply',payload,remote,drain)
        self.assertEqual(calls,['status','check','drain','set'])

    def test_stale_and_failed_drain_never_apply(self):
        calls=[]
        def remote(op,data):calls.append(op);return {'config':{'revision':4}}
        def drain(data):raise RuntimeError('hardware drain failed')
        with self.assertRaises(ValueError):execute('apply',{'operation':'stop','revision':3},remote,drain)
        self.assertEqual(calls,['status']);calls.clear()
        with self.assertRaises(RuntimeError):execute('apply',{'operation':'start','revision':4},remote,drain)
        self.assertEqual(calls,['status'])


if __name__=='__main__':unittest.main()
