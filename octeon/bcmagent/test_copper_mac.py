import unittest
from unittest.mock import Mock
import ffn_bcm_copper as copper
class CopperMacTests(unittest.TestCase):
    def test_unknown_ports_and_rates_never_reach_sdk(self):
        for request in ({'port':16,'speed_mbps':1000},{'port':True,'speed_mbps':1000},{'port':13,'speed_mbps':'1000'},{'port':13,'speed_mbps':2500}):
            chip=Mock()
            with self.assertRaises(ValueError):copper.apply(chip,request)
            chip.run.assert_not_called()
    def test_sync_1g_selects_sgmii_and_preserves_enabled_state(self):
        chip=Mock();chip.run.side_effect=['FFNCOPPER 0 1 10000 0 1 0 1','FFNCOPPERSET 0 0','FFNCOPPER 0 1 1000 0 1 1 0']
        result=copper.apply(chip,{'port':13,'speed_mbps':1000})
        self.assertEqual(result['interface'],'sgmii')
        cmd=chip.run.call_args_list[1].args[0]
        self.assertTrue(cmd.startswith('cint\n'))
        self.assertIn('bcm_port_interface_set(0,13,BCM_PORT_IF_SGMII)',cmd)
        self.assertIn('restore=bcm_port_enable_set(0,13,1)',cmd)
    def test_stable_mac_requires_no_writes(self):
        chip=Mock();chip.run.return_value='FFNCOPPER 0 1 1000 0 1 1 0'
        copper.apply(chip,{'port':13,'speed_mbps':1000})
        self.assertEqual(chip.run.call_count,1)
    def test_failed_change_or_restore_is_not_success(self):
        for reply in ('FFNCOPPERSET -1 0','FFNCOPPERSET 0 -1'):
            chip=Mock();chip.run.side_effect=['FFNCOPPER 0 1 10000 0 1 0 1',reply]
            with self.assertRaises(RuntimeError):copper.apply(chip,{'port':13,'speed_mbps':1000})
    def test_disabled_mac_is_not_enabled_by_sync(self):
        chip=Mock();chip.run.return_value='FFNCOPPER 0 0 10000 0 1 0 1'
        with self.assertRaises(ValueError):copper.apply(chip,{'port':13,'speed_mbps':1000})
        self.assertEqual(chip.run.call_count,1)
if __name__=='__main__':unittest.main()
