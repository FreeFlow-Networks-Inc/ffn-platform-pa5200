import unittest
import json
from pathlib import Path
import tempfile
from ffn_fe100_clocks import SHA
from ffn_fe100_live_sessions import prerequisites, calibration_journals, action_prerequisites, pipeline_prerequisites


class SessionPrerequisites(unittest.TestCase):
    def test_ready_doorbell_does_not_hide_pipeline_fault_or_busy_ia(self):
        values = {0x48018:1, 0x40200:0x4300, 0x48080:0x4300,
                  0x70500:0x4300, 0x4080c:1 << 23}
        self.assertEqual(pipeline_prerequisites(values), [])
        for register in (0x40200, 0x48080, 0x70500):
            for error in (0x1000, 0x2000):
                self.assertTrue(pipeline_prerequisites(values | {register:0x4300 | error}))
        for code in (0, 2, 3, 4, 5, 6, 7):
            self.assertTrue(pipeline_prerequisites(values | {0x4080c:code << 23}))
        self.assertTrue(pipeline_prerequisites({}))

    def setUp(self):
        self.values = {0x40010:0xfffff,0x40014:0,0x40404:0x1e000,
                       0x40400:0x2000,0x48708:7,0x48018:1}
        for base in (0xa8000,0xb0000):
            self.values.update({base+0x134:1,base+0x100:3,
                base+0x148:0x4d1,base+0x168:0x4d1,
                base+0x150:0x4201f011,base+0x170:0x4201f011})
        self.journals = {c:dict(cp_boot_id='boot',stage='completed',owner_sha256=SHA,faults=0)
                         for c in (3,4,5,6)}

    def test_ready_requires_all_channels(self):
        self.assertEqual(prerequisites(self.values,'boot',self.journals),[])
        for c in self.journals:
            missing = dict(self.journals); del missing[c]
            self.assertTrue(prerequisites(self.values,'boot',missing))

    def test_actions_require_counter_memory_and_current_journals(self):
        values={0x98008:3,0x98174:1,0x98128:0x4d1,0x98148:0x4d1,
                0x98130:1,0x98150:1,0x78804:0x1000e0,0x406a4:0x700070}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.assertTrue(action_prerequisites(values,root,'boot'))
            for stage in ('fcm-clocks','fcm-train-0','fcm-train-1','sem-init'):
                path=root/(stage+'-boot.json')
                path.write_text(json.dumps(self.journals[3]))
            self.assertEqual(action_prerequisites(values,root,'boot'),[])
            for reg in values:
                bad=values | {reg:0}
                self.assertTrue(action_prerequisites(bad,root,'boot'),hex(reg))
            for raw in ('{','[]',json.dumps(dict(self.journals[3],stage='started')),
                        json.dumps(dict(self.journals[3],cp_boot_id='old')),
                        json.dumps(dict(self.journals[3],faults=1))):
                path.write_text(raw)
                self.assertTrue(action_prerequisites(values,root,'boot'))

    def test_failed_calibration_overrides_good_csr(self):
        # Actual FDT1 failure still reported DPHY=0x3cd1 and normal UMCTL.
        self.values[0xb0168] = 0x3cd1
        self.journals[6]['stage'] = 'failed'
        self.assertIn('fdt1 lacks successful calibration in this boot',
                      prerequisites(self.values,'boot',self.journals))

    def test_stale_unknown_and_faulted_records_rejected(self):
        for field,value in (('cp_boot_id','old'),('stage','started'),('faults',1),('owner_sha256','other')):
            records = {c:dict(r) for c,r in self.journals.items()}
            records[3][field] = value
            self.assertTrue(prerequisites(self.values,'boot',records))

    def test_missing_controller_or_processor_readiness(self):
        for register in (0x40010,0x48708,0x48018,0xa8134,0xb0170):
            values = dict(self.values); values[register] = 0
            self.assertTrue(prerequisites(values,'boot',self.journals),hex(register))

    def test_native_cfg4_normal_mode(self):
        self.values.update({0x40010:0x7ff,0x40404:0x120,0x40400:0x8104c729})
        self.assertEqual(prerequisites(self.values,'boot',self.journals),[])
        for bit in range(11):
            values=dict(self.values);values[0x40010]&=~(1<<bit)
            self.assertIn('FLU memory initialization is incomplete',prerequisites(values,'boot',self.journals))

    def test_controller_recovery_failure_supersedes_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for stage in ('train-6','recover-calibration-6'):
                (root/('fdt-'+stage+'-boot.json')).write_text(json.dumps(self.journals[6]))
            latest=root/'fdt-recover-controller-6-boot.json'
            for content in (json.dumps(dict(self.journals[6],stage='failed')), '{', '[]',
                            json.dumps(dict(self.journals[6],stage='started'))):
                latest.write_text(content)
                records=calibration_journals(root,'boot')
                self.assertEqual(records[6]['journal'],str(latest))
                self.assertIn('fdt1 lacks successful calibration in this boot',
                              prerequisites(self.values,'boot',self.journals | records))

    def test_pattern_recovery_can_resolve_calibration_but_not_flu(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'fdt-recover-controller-6-boot.json').write_text(
                json.dumps(dict(self.journals[6],stage='failed')))
            latest=root/'fdt-recover-init-pattern-6-boot.json'
            latest.write_text(json.dumps(self.journals[6]))
            records=self.journals | calibration_journals(root,'boot')
            self.assertEqual(prerequisites(self.values,'boot',records),[])
            self.values[0x40010]=0
            self.assertEqual(prerequisites(self.values,'boot',records),
                             ['FLU memory initialization is incomplete'])
            latest.write_text('{')
            records=self.journals | calibration_journals(root,'boot')
            self.assertIn('fdt1 lacks successful calibration in this boot',
                          prerequisites(self.values,'boot',records))


if __name__ == '__main__': unittest.main()
