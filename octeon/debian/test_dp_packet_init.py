import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ffn_dp_packet_init import prepare, status, set_trunk


class PacketInit(unittest.TestCase):
    def test_trunk_transition_fixed_argv_and_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {'schema':1,'ready':False,'trunk':{
                'interface':'ffnpkt0','running':False,'error':0}}
            (root/'status').write_text(json.dumps(base))
            calls = []
            def run(argv, **kwargs):
                calls.append((argv, kwargs))
                base['trunk']['running'] = argv[-1] == 'up'
                (root/'status').write_text(json.dumps(base))
            self.assertTrue(set_trunk(True,root,root/'lock',run)['trunk']['running'])
            self.assertFalse(set_trunk(False,root,root/'lock',run)['trunk']['running'])
            self.assertEqual(calls[0],(['ip','link','set','dev','ffnpkt0','up'],
                                       {'check':True,'timeout':15}))

    def test_trunk_fault_and_missing_registration_do_not_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for t in ({}, {'interface':'ffnpkt0','error':-5}):
                (root/'status').write_text(json.dumps({'schema':1,'ready':False,'trunk':t}))
                with self.assertRaises(RuntimeError):
                    set_trunk(True,root,root/'lock',lambda *a,**k:self.fail('unexpected command'))

    def test_nonquiescent_or_incomplete_boot_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {'schema':1, 'ready':False, 'pki_reset_busy':False,
                    'pki_active':0, 'pki_enabled':0, 'pko_enabled':0}
            for field in ('pki_active','pki_enabled','pko_enabled','pki_reset_busy'):
                (root/'status').write_text(json.dumps({**base,field:1}))
                with self.assertRaises(RuntimeError):
                    prepare(root, root/'lock', lambda:{'ready':True})
                self.assertFalse((root/'prepare').exists())
            (root/'status').write_text(json.dumps(base))
            with self.assertRaises(RuntimeError):
                prepare(root, root/'lock', lambda:{'ready':False})
            self.assertFalse((root/'prepare').exists())

    def test_unknown_abi_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'status').write_text(json.dumps({'schema':2, 'ready':False}))
            with self.assertRaises(RuntimeError): status(root)

    def test_failed_dma_and_missing_pool_prevent_queue_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            base={'schema':1,'ready':False,'pki_reset_busy':False,
                  'pki_active':0,'pki_enabled':0,'pko_enabled':0}
            for extra in ({'dma_error':-5}, {'dma_ready':False}):
                (root/'status').write_text(json.dumps({**base,**extra}))
                for op in ('prepare-sso','prepare-pko-memory'):
                    with self.assertRaises(RuntimeError):
                        prepare(root,root/'lock',lambda:{'ready':True},op)
                    self.assertFalse((root/'prepare').exists())

    def test_hardware_write_is_never_retried_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'status').write_text(json.dumps({'schema':1,'ready':False,
                'pki_reset_busy':False,'pki_active':0,'pki_enabled':0,'pko_enabled':0}))
            (root/'prepare').touch()
            with patch('ffn_dp_packet_init.os.write',side_effect=OSError(5,'hardware')) as write:
                with self.assertRaises(OSError):
                    prepare(root,root/'lock',lambda:{'ready':True},'prepare-dma')
                write.assert_called_once()


if __name__ == '__main__': unittest.main()
