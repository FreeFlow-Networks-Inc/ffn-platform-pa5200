import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock
from ffn_dp_link import enable_once, report, observe


class Link(unittest.TestCase):
    def setUp(self):
        self.down=dict(schema=1,bgx=2,lmac=0,lmac_type=4,enabled=0,rx_enabled=0,tx_enabled=0,
                       link=0,block_lock=0,fault=0,pknd=0,pki_enabled=0)
        self.up=dict(self.down,enabled=1,rx_enabled=1,tx_enabled=1,link=1,block_lock=1,pknd=8)

    def test_enable_then_verify(self):
        write=Mock(); result=enable_once(Mock(side_effect=[self.down,self.up]),write)
        write.assert_called_once_with('2\n')
        self.assertTrue(result['internal_link_ready'])
        self.assertFalse(result['physical_packet_transport_verified'])

    def test_no_reinitialize_live_link(self):
        write=Mock(); result=enable_once(lambda:self.up,write)
        self.assertFalse(result['changed']); write.assert_not_called()

    def test_reject_busy_wrong_mode_and_partial_state(self):
        for state in (dict(self.down,pki_enabled=1),dict(self.down,lmac_type=0),dict(self.down,tx_enabled=1)):
            write=Mock()
            with self.assertRaises((ValueError,RuntimeError)): enable_once(lambda:state,write)
            write.assert_not_called()

    def test_failures_not_success(self):
        result=enable_once(Mock(side_effect=[self.down,self.up]),Mock(side_effect=OSError()))
        self.assertIn('error',result); self.assertIsNone(result['changed'])
        for state in (dict(self.up,pknd=63),dict(self.up,link=0),dict(self.up,fault=1)):
            self.assertFalse(report(state)['internal_link_ready'])

    def test_missing_invalid_and_fresh_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status'
            self.assertIsNone(observe(path)['internal_link_ready'])
            for raw in ('[]', '{', 'x'*8193):
                path.write_text(raw)
                self.assertFalse(observe(path)['available'])
            path.write_text(json.dumps(self.up))
            self.assertTrue(observe(path)['internal_link_ready'])


if __name__=='__main__': unittest.main()
