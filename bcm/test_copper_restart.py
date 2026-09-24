import unittest
from unittest.mock import Mock
from ffn_84848_boot import Copper

class RestartTests(unittest.TestCase):
    def test_enabled_phy_does_not_renegotiate(self):
        bus=Mock();values={(30,0x400f):0x1089,(1,0):0,(30,0x401a):0,(7,0):0x3000}
        bus.transfer.side_effect=lambda phy,dev,reg:values[dev,reg]
        Copper(bus,17).enable()
        self.assertTrue(all(len(c.args)==3 for c in bus.transfer.call_args_list))
    def test_invalid_firmware_is_rejected(self):
        bus=Mock();bus.transfer.return_value=65535
        with self.assertRaises(RuntimeError):Copper(bus,17).enable()
    def test_new_firmware_stays_disabled_without_autoneg_restart(self):
        bus=Mock();values={(30,0x400f):0x1089,(1,0):0,(30,0x400e):0,(30,0x401a):0x8105}
        def transfer(phy,dev,reg,*write):
            if write:values[dev,reg]=write[0]
            return values[dev,reg]
        bus.transfer.side_effect=transfer
        Copper(bus,17).prepare_disabled()
        self.assertEqual(values[30,0x401a],0x85)
        writes=[c.args for c in bus.transfer.call_args_list if len(c.args)==4]
        self.assertEqual(writes,[(17,30,0x401a,0x85)])
    def test_disabled_firmware_setup_is_idempotent(self):
        bus=Mock();values={(30,0x400f):0x1089,(1,0):0,(30,0x400e):0,(30,0x401a):0x80}
        bus.transfer.side_effect=lambda phy,dev,reg:values[dev,reg]
        Copper(bus,17).prepare_disabled()
        self.assertTrue(all(len(c.args)==3 for c in bus.transfer.call_args_list))
if __name__=='__main__':unittest.main()
