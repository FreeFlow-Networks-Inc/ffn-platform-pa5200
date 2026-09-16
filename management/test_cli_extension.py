import contextlib
import io
import unittest
from unittest.mock import Mock
from cli_extension import handle

class CLITests(unittest.TestCase):
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
