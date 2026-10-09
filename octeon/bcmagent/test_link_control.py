import unittest
from unittest.mock import Mock
import ffn_bcm_link as link

class LinkTests(unittest.TestCase):
    def test_supported_modes_are_board_filtered(self):
        chip=Mock();chip.run.return_value='FFNLINK 0 1 10000 10\nFFNSPEED 1000\nFFNSPEED 10000\nFFNSPEED 40000\nFFNSPEED 100000'
        self.assertEqual(link.status(chip,{'port':16})['supported_speeds'],[1000,10000])
        self.assertEqual(link.status(chip,{'port':34})['supported_speeds'],[40000,100000])
    def test_echoed_cint_does_not_advertise_unsupported_speeds(self):
        chip=Mock()
        chip.run.return_value='cint> if (a.speed_full_duplex & BCM_PORT_ABILITY_1000MB) printf("FFNSPEED 1000\\n");\nFFNLINK 0 0 10000 10\nFFNSPEED 10000\nFFNSPEED 10000\n'
        self.assertEqual(link.status(chip,{'port':16})['supported_speeds'],[10000])
    def test_fixed_speed_checked_and_read_back(self):
        chip=Mock();chip.run.side_effect=['FFNLINK 0 1 10000 10\nFFNSPEED 1000\nFFNSPEED 10000','FFNSET 0','FFNLINK 0 0 1000 4\nFFNSPEED 1000']
        self.assertEqual(link.apply(chip,{'port':16,'speed':'1000'})['configured_speed'],'1000')
        command=chip.run.call_args_list[1].args[0]
        self.assertIn('bcm_port_autoneg_set(0,16,0)',command)
        self.assertIn('bcm_port_speed_set(0,16,1000)',command)
    def test_auto_does_not_overwrite_advertisement(self):
        chip=Mock();chip.run.side_effect=['FFNLINK 0 0 10000 10\nFFNSPEED 10000','FFNSET 0','FFNLINK 0 1 0 10\nFFNSPEED 10000']
        self.assertEqual(link.apply(chip,{'port':16,'speed':'auto'})['configured_speed'],'auto')
        self.assertNotIn('ability_advert_set',chip.run.call_args_list[1].args[0])
    def test_invalid_port_and_injection_never_reach_sdk(self):
        for port in (True,12,28,'16;exit;'):
            chip=Mock()
            with self.assertRaises(ValueError):link.apply(chip,{'port':port,'speed':'1000'})
            chip.run.assert_not_called()
    def test_unsupported_speed_does_not_write(self):
        chip=Mock();chip.run.return_value='FFNLINK 0 1 10000 10\nFFNSPEED 10000'
        with self.assertRaises(ValueError):link.apply(chip,{'port':16,'speed':'40000'})
        self.assertEqual(chip.run.call_count,1)
    def test_failed_sdk_or_mismatched_readback_never_succeeds(self):
        for replies in (['FFNSET -1'],['FFNSET 0','FFNLINK 0 1 10000 10\nFFNSPEED 1000']):
            chip=Mock();chip.run.side_effect=['FFNLINK 0 1 10000 10\nFFNSPEED 1000']+replies
            with self.assertRaises(RuntimeError):link.apply(chip,{'port':16,'speed':'1000'})
    def test_1000base_x_autoneg_is_a_configured_1000_not_auto(self):
        # Clause 37 negotiates duplex and pause, never speed: GMII with
        # autonegotiation reads back as the fixed 1000 the applier requested.
        chip=Mock();chip.run.return_value='FFNLINK 0 1 1000 3\nFFNSPEED 1000\nFFNSPEED 10000'
        result=link.status(chip,{'port':16})
        self.assertEqual((result['configured_speed'],result['autoneg'],result['interface'],result['link_mode']),('1000',True,3,'1000BASE-X'))
        chip.run.return_value='FFNLINK 0 0 1000 4\nFFNSPEED 1000\nFFNSPEED 10000'
        self.assertEqual((link.status(chip,{'port':16})['configured_speed'],link.status(chip,{'port':16})['link_mode']),('1000','SGMII'))
        chip.run.return_value='FFNLINK 0 1 0 10\nFFNSPEED 10000'
        self.assertEqual(link.status(chip,{'port':16})['configured_speed'],'auto')
if __name__=='__main__':unittest.main()
