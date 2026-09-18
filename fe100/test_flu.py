import json
from pathlib import Path
import struct
import tempfile
import unittest
from ffn_fe100_flu import configuration, completed_targets, OFFSETS
from ffn_fe100_live_sessions import flu_record
from ffn_fe100_clocks import SHA


class FluTests(unittest.TestCase):
    def setUp(self):
        self.records={}
        for channel,off in OFFSETS.items():
            raw=bytearray(2812)
            struct.pack_into('>I',raw,off+108,channel)
            struct.pack_into('>I',raw,48,4 if channel==6 else 3)
            self.records[channel]={'stage':'completed','owner_sha256':SHA,'cp_boot_id':'boot',
                                   'faults':0,'configuration_hex':raw.hex()}

    def test_rehydrate_each_channel_and_detected_capacity(self):
        cfg=configuration(bytes(2812),self.records,'boot')
        self.assertEqual(struct.unpack_from('>I',cfg,48)[0],4)
        self.assertEqual(struct.unpack_from('>II',cfg,1400),(4,2))
        for channel,off in OFFSETS.items():
            self.assertEqual(struct.unpack_from('>I',cfg,off+108)[0],channel)

    def test_bad_or_stale_training_cannot_initialize(self):
        for field,value in [('stage','failed'),('cp_boot_id','old'),('faults',1),('owner_sha256','other')]:
            records={c:dict(r) for c,r in self.records.items()};records[6][field]=value
            with self.assertRaises(ValueError): configuration(bytes(2812),records,'boot')

    def test_vref_sweep_not_silently_enabled(self):
        raw=bytearray.fromhex(self.records[5]['configuration_hex'])
        struct.pack_into('>I',raw,716+184,1);self.records[5]['configuration_hex']=raw.hex()
        with self.assertRaises(ValueError): configuration(bytes(2812),self.records,'boot')

    def trace(self):
        return '\n'.join(f'W 0x40800 {0x08000001|(2<<i):#x}\nR 0x4080c 0x0\nR 0x4080c 0x800000' for i in range(11))

    def test_completion_requires_all_targets_not_submissions(self):
        trace=self.trace()
        self.assertEqual(completed_targets(trace),[1<<i for i in range(11)])
        for bad in (trace.rsplit('\n',1)[0], trace.replace('R 0x4080c 0x800000','R 0x4080c 0x1000000',1),
                    trace.replace('R 0x4080c 0x800000','',1)):
            with self.assertRaises(RuntimeError): completed_targets(bad)

    def test_failed_verification_overrides_prior_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r={'stage':'completed','cp_boot_id':'boot','owner_sha256':SHA,'faults':0}
            (root/'flu-init-boot.json').write_text(json.dumps(r))
            self.assertIsNotNone(flu_record(root,'boot'))
            (root/'flu-verified-boot.json').write_text('{')
            self.assertIsNone(flu_record(root,'boot'))


if __name__=='__main__': unittest.main()
