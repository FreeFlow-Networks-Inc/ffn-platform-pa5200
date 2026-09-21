import contextlib
import io
import json
import unittest
from unittest.mock import Mock
from cli_extension import handle, complete

class CLITests(unittest.TestCase):
    def test_fe100_views_never_mutate_and_preserve_freshness(self):
        api=Mock(return_value={'agents':{'cp':{'role':'cp','fresh':False,'age_seconds':91,
            'last_observation':{'report':{'fe100':{'offload_verified':True,'counters':{'received':10}},
            'fe100_driver':{'userspace':{'read_verified':True}},
            'policy':{'hardware_activation_verified':True,'recovery':{'drain_verified':True}}}}}}})
        for view in ('status','driver','counters','policy','recovery'):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                handle('show platform fe100 '+view,api,'session')
            self.assertIn('STALE / UNAVAILABLE',output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            handle('show platform fe100 status json',api,'session')
        status=json.loads(output.getvalue())['cp']['status']
        self.assertFalse(status['drain_verified'])
        self.assertFalse(status['hardware_activation_verified'])
        self.assertFalse(status['forwarding_verified'])
        self.assertEqual(status['register_access'],'unverified')
        for call in api.call_args_list:
            self.assertEqual(call.args,('/api/system/control',))
            self.assertEqual(call.kwargs,{'token':'session'})

    def test_help_completion_invalid_command_and_missing_data(self):
        api=Mock()
        for command in ('help platform','help platform fe100','? platform'):
            with contextlib.redirect_stdout(io.StringIO()) as output:handle(command,api,'session')
            self.assertIn('recovery',output.getvalue())
        self.assertEqual(complete('show platform fe100 ','re'),['recovery'])
        self.assertEqual(complete('show platform fe100 recovery ','j'),['json'])
        for command in ('show platform fe100 reset','show platform fe100 recovery extra','show platform fe100 json json'):
            with self.assertRaises(ValueError):handle(command,api,'session')
        api.assert_not_called()
        api.return_value={'agents':{}}
        with contextlib.redirect_stdout(io.StringIO()) as output:handle('show platform fe100 status',api,'session')
        self.assertIn('unavailable',output.getvalue())
        api.return_value={'agents':{'cp':{'role':'cp','fresh':False,'last_observation':None}}}
        with contextlib.redirect_stdout(io.StringIO()) as output:handle('show platform fe100 status',api,'session')
        self.assertIn('unknown',output.getvalue())

    def test_fe100_shows_recovery_and_transport_freshness_from_controld(self):
        api=Mock(return_value={'agents':{'cp':{'role':'cp','fresh':False,'age_seconds':91,
            'last_observation':{'report':{'fe100':{'available':True},
            'fe100_driver':{'forwarding_verified':False},
            'policy':{'recovery':{'outcome':'blocked','drain_verified':False}}}}}}})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(handle('show platform fe100',api,'session'))
        result=json.loads(output.getvalue())['cp']
        self.assertFalse(result['fresh'])
        self.assertFalse(result['policy']['recovery']['drain_verified'])
        self.assertFalse(result['driver']['forwarding_verified'])
        api.assert_called_once_with('/api/system/control',token='session')

    def test_none_mode_stages_candidate_and_preserves_explicit_link_settings(self):
        for mode in ('none','off','disabled'):
            api=Mock(side_effect=[{'ethernet':[{'name':'ethernet1/2','mode':'layer3','link_state':'down','link_speed':'1000','comment':'Spare'}]},
                                  {'status':'created'}])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertTrue(handle('request platform interface ethernet1/2 mode '+mode,api,'session'))
            api.assert_called_with('/api/interfaces/ethernet1%2F2',method='PUT',token='session',
                body={'name':'ethernet1/2','mode':'none','link_state':'down','link_speed':'1000','comment':'Spare'})
            self.assertIn('"requires_commit": true',output.getvalue())
        api=Mock()
        with self.assertRaises(ValueError):handle('request platform interface mgt mode none',api,'session')
        api.assert_not_called()
    def test_wan_probe_uses_observed_revision_and_dp_identity(self):
        api=Mock(side_effect=[{'ok':True,'result':{'revision':8,'dp':{'boot_id':'current'}}},
                              {'ok':True,'result':{'qualified':False}}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle('request platform wan-path probe',api,'session'))
        call=api.call_args
        self.assertEqual(call.args,('/api/system/planes',))
        self.assertEqual(call.kwargs['token'],'session')
        self.assertEqual(call.kwargs['body']['resource'],'wan-path')
        self.assertEqual(call.kwargs['body']['payload'],{'revision':8,'operation':'probe','expected_boot_id':'current'})
        with self.assertRaises(ValueError):handle('request platform wan-path shell',api,'session')
    def test_copper_recovery_and_renegotiation_use_authenticated_mp_route(self):
        for action,field in [('recover-pairs','restore_pair_map'),('renegotiate','restart_autoneg')]:
            api=Mock(side_effect=[{'revision':42},{'activation':'verified'}])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(handle('request platform interface ethernet1/4 '+action,api,'session'))
            api.assert_called_with('/api/system/runtime/faceplate/set',method='POST',token='session',body={'revision':42,'port':4,field:True})
            api.reset_mock()
            with self.assertRaises(ValueError):handle('request platform interface ethernet1/5 '+action,api,'session')
            api.assert_not_called()
    def test_interface_speed_uses_mp_api_and_current_revision(self):
        api=Mock(side_effect=[{'revision':42},{'activation':'verified'}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle('request platform interface ethernet1/5 link-speed 1000',api,'session'))
        api.assert_called_with('/api/system/runtime/faceplate/set',method='POST',token='session',body={'revision':42,'port':5,'speed':'1000'})
    def test_shared_authenticated_path(self):
        api=Mock(return_value={'control':{'trace':['mp']}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle('show platform faceplate',api,'session'))
            api.assert_called_with('/api/system/runtime/faceplate',token='session')
            self.assertTrue(handle('request platform faceplate set \'{"revision":7,"port":1,"enabled":false}\'',api,'session'))
            api.assert_called_with('/api/system/runtime/faceplate/set',method='POST',body={'revision':7,'port':1,'enabled':False},token='session')
        self.assertFalse(handle('show system info',api,'session'))
        with self.assertRaises(ValueError):handle('request platform shell run {}',api,'session')

if __name__=='__main__':unittest.main()
