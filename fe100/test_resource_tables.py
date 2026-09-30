import unittest
from unittest.mock import Mock,patch
from ffn_fe100_resource_tables import ResourceTables


class NativeBoundary(unittest.TestCase):
    def setUp(self):
        self.io=ResourceTables.__new__(ResourceTables)
        self.io.pools={'smac':[30,31],'nexthop':[30,31]};self.io.uncertain=set()
        self.process=Mock();self.process.poll.return_value=None
        self.lines=Mock();self.lines.read.return_value={'rc':0,'data':'00'*8}
        self.io.workers={'smac':(self.process,Mock(),self.lines)}
        self.io.stop=Mock(side_effect=lambda k:self.io.workers.pop(k,None))
        self.send=patch('ffn_fe100_resource_tables.send').start();self.addCleanup(patch.stopall)
    def test_unknown_pool_index_never_contacts_worker(self):
        for index in (29,32,True):
            with self.assertRaises(ValueError):self.io.fetch('smac',index)
        self.send.assert_not_called()
    def test_timeout_fences_same_entry_until_successful_readback(self):
        self.lines.read.side_effect=TimeoutError('lost ack')
        with self.assertRaises(TimeoutError):self.io.insert('smac',30,b'\0'*8)
        with self.assertRaises(RuntimeError):self.io.insert('smac',30,b'\0'*8)
        self.io.workers={'smac':(self.process,Mock(),self.lines)};self.lines.read.side_effect=None
        self.io.fetch('smac',31)
        with self.assertRaises(RuntimeError):self.io.delete('smac',30)
        self.io.fetch('smac',30);self.io.delete('smac',30)
        self.assertFalse(self.io.uncertain)
    def test_native_not_found_is_only_successful_for_fetch(self):
        self.lines.read.return_value={'rc':3,'data':'00'*8}
        self.assertIsNone(self.io.fetch('smac',30))
        with self.assertRaises(RuntimeError):self.io.delete('smac',30)
        self.assertIn(('smac',30),self.io.uncertain)
    def test_partial_response_does_not_acknowledge_write(self):
        self.lines.read.return_value={'rc':0,'data':'00'}
        with self.assertRaises(RuntimeError):self.io.insert('smac',30,b'\0'*8)
        self.assertIn(('smac',30),self.io.uncertain)


if __name__=='__main__':unittest.main()
