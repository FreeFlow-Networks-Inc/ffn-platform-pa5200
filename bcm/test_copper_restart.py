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
if __name__=='__main__':unittest.main()
