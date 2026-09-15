import unittest
from vif_backend import execute


class Backend(unittest.TestCase):
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
