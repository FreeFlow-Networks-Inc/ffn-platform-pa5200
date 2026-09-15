import ctypes as C
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from ffn_fe100_external_runtime import ExternalTransport,verify_live
from ffn_fe100_external_tables import cfg4_v4_v6


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'external.json'
        self.io=Mock(trace='trace.txt')
        self.io.shim.ffn_fe100_allow_external_ia.return_value=0
        self.io.shim.ffn_fe100_allow_readonly.return_value=0
        self.io.shim.ffn_fe100_allow.return_value=0
        self.io.shim.ffn_fe100_faults.return_value=0
        self.transport=ExternalTransport(self.io,self.path)

    def test_failed_readback_is_persisted(self):
        self.transport.begin('cfg4-v4-v6',263)
        self.io.lib.fe100_ia_op.return_value=12
        self.assertFalse(self.transport.verify_configuration(cfg4_v4_v6()))
        self.transport.failed(263)
        result=json.loads(self.path.read_text())
        self.assertEqual(result['stage'],'failed')
        self.assertEqual(result['completed'],263)
        self.assertFalse(result['session_offload_verified'])
        self.assertEqual(result['readback_mismatch']['address'],1)

    def test_wrong_target_never_reaches_native(self):
        req,data=cfg4_v4_v6()[0].native()
        req.trgt_mem=0x999
        with self.assertRaises(ValueError): self.transport.ia_op(0,16,req)
        self.io.lib.fe100_ia_op.assert_not_called()

    def test_mmio_fault_stops_transaction(self):
        req,data=cfg4_v4_v6()[0].native()
        self.io.shim.ffn_fe100_faults.return_value=1
        with self.assertRaises(RuntimeError): self.transport.ia_op(0,16,req)

    def test_zero_return_without_completion_is_failure(self):
        req,data=cfg4_v4_v6()[0].native()
        self.io.lib.fe100_ia_op.return_value=0
        self.assertNotEqual(self.transport.ia_op(0,16,req),0)
        req.cc=1
        self.assertEqual(self.transport.ia_op(0,16,req),0)

    def setup_live(self):
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.path.write_text(json.dumps({'stage':'configuration-verified','cp_boot_id':boot}))
        values={0xa01c0:0x03f00000,0x80508:0x00800000,0xa0708:3}
        self.io.read.side_effect=lambda r:values.get(r,0)
        entries={entry.address:entry.words for entry in cfg4_v4_v6()}
        def read(dev,block,ref):
            req=ref._obj
            self.assertEqual(req.acc_type,1)
            req.cc=1
            for index,value in enumerate(entries[req.addr]): req.data[index]=value
            return 0
        self.io.lib.fe100_ia_op.side_effect=read
        return values

    def test_live_verify_all_entries_without_configuration_writes(self):
        self.setup_live()
        with patch('ffn_fe100_external_runtime.JOURNAL',self.path),patch(
                'ffn_fe100_external_runtime.DDRRegisters',return_value=self.io):
            result=verify_live()
        self.assertEqual(result['entries_verified'],263)
        self.assertFalse(result['recovery_required'])
        self.assertEqual(self.io.lib.fe100_ia_op.call_count,263)

    def test_pending_read_not_overwritten_by_verification(self):
        values=self.setup_live(); values[0x80508]=0
        with patch('ffn_fe100_external_runtime.JOURNAL',self.path),patch(
                'ffn_fe100_external_runtime.DDRRegisters',return_value=self.io):
            with self.assertRaises(RuntimeError): verify_live()
        self.io.lib.fe100_ia_op.assert_not_called()

    def test_live_verify_failure_persisted(self):
        self.setup_live(); self.io.lib.fe100_ia_op.side_effect=lambda *_:12
        with patch('ffn_fe100_external_runtime.JOURNAL',self.path),patch(
                'ffn_fe100_external_runtime.DDRRegisters',return_value=self.io):
            with self.assertRaises(RuntimeError): verify_live()
        result=json.loads(self.path.read_text())
        self.assertEqual(result['stage'],'verification-failed')
        self.assertTrue(result['recovery_required'])


if __name__=='__main__': unittest.main()
