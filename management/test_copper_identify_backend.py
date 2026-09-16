import unittest
from unittest.mock import patch
import copper_identify_backend as backend


class Migration(unittest.TestCase):
    def test_dp_preflight_precedes_correction_and_cp_commits_last(self):
        calls=[]
        def cp(action,payload):calls.append(('cp',action));return {'mapping':{'1':{'phy':17,'bcm_port':28}}}
        def dp(mapping,write):calls.append(('dp',write))
        request={'operation':'correct-wan-label','revision':1}
        with patch.object(backend,'cp',side_effect=cp),patch.object(backend,'dp_profile',side_effect=dp):
            backend.execute('validate',request)
            self.assertEqual(calls,[('cp','validate'),('dp',False)])
            calls.clear();backend.execute('apply',request)
            self.assertEqual(calls,[('cp','validate'),('dp',True),('cp','apply')])

    def test_blocked_dp_profile_never_changes_cp_mapping(self):
        with patch.object(backend,'cp',return_value={'mapping':{}}) as cp,\
             patch.object(backend,'dp_profile',side_effect=ValueError('assigned VIF')):
            with self.assertRaises(ValueError):backend.execute('apply',{'operation':'correct-wan-label','revision':1})
            self.assertEqual(cp.call_count,1)
            self.assertEqual(cp.call_args.args[0],'validate')


if __name__=='__main__':unittest.main()
