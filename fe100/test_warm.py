import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from dataclasses import asdict
from ffn_fe100_config import Profile
from ffn_fe100_clocks import SHA
import ffn_fe100_warm as warm


class WarmTests(unittest.TestCase):
    def test_live_queue_credits_do_not_hide_controller_or_sticky_fault_changes(self):
        from ffn_fe100_ddr_diagnostics import stable_snapshot
        self.assertEqual(stable_snapshot({'0xa8150':0x4201d011}),stable_snapshot({'0xa8150':0x4201f011}))
        self.assertNotEqual(stable_snapshot({'0xa8150':0x4201f011}),stable_snapshot({'0xa8150':0x6201f011}))
        self.assertNotEqual(stable_snapshot({'0xa8150':0x4201f011}),stable_snapshot({'0xa8150':0x4201f012}))

    def test_calibration_errors_and_incomplete_groups_block(self):
        def report():
            return dict(block='fcm',faults=0,memory_written=False,before={'0x98130':1},after={'0x98130':1},channels=[
                dict(channel=c,calibration_registers={'0x18':0,'0x19':0x8000},groups=[
                    dict(group=0,registers={'0x14':0,'0x17':0xf080},measurements_present=True)]) for c in (0,1)])
        self.assertEqual(warm.calibration_errors(report()),[])
        for mutate in (lambda r:r.update(faults=1),lambda r:r.update(memory_written=True),
            lambda r:r['channels'].pop(),lambda r:r['after'].update({'0x98130':2}),
            lambda r:r['channels'][0]['calibration_registers'].update({'0x18':0x808}),
            lambda r:r['channels'][0]['calibration_registers'].update({'0x19':0}),
            lambda r:r['channels'][0]['groups'].clear(),
            lambda r:r['channels'][0]['groups'][0]['registers'].update({'0x14':1}),
            lambda r:r['channels'][0]['groups'][0].update(measurements_present=False)):
            r=report();mutate(r);self.assertTrue(warm.calibration_errors(r))

    def test_grant_expires_and_cannot_survive_boot_profile_source_or_csr_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.json';source.write_text('original')
            digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
            values={r:1 for r in warm.STABLE};path=root/'warm-lab-boot.json'
            record=dict(schema=1,stage='verified',scope=warm.SCOPE,cp_boot_id='boot',owner_sha256=SHA,
                production_admission=False,monotonic=100,stable={hex(r):1 for r in values},
                profile=asdict(Profile()),sources={source.name:digest(source)})
            def save():path.write_text(json.dumps(record))
            save()
            with patch.object(warm,'fingerprint',side_effect=digest),patch('ffn_fe100_config.load_profile',return_value=Profile()):
                self.assertTrue(warm.grant_valid(root,'boot',values,100))
                for now in (99,701):self.assertFalse(warm.grant_valid(root,'boot',values,now))
                self.assertFalse(warm.grant_valid(root,'other',values,100))
                self.assertFalse(warm.grant_valid(root,'boot',dict(values,**{})|{warm.STABLE[0]:0},100))
                source.write_text('changed');self.assertFalse(warm.grant_valid(root,'boot',values,100));source.write_text('original')
                for key,bad in [('stage','started'),('production_admission',True),('scope','production'),('owner_sha256','other'),('profile',{}),('sources',{})]:
                    old=record[key];record[key]=bad;save();self.assertFalse(warm.grant_valid(root,'boot',values,100));record[key]=old

    def test_lab_authorization_does_not_authorize_production_or_other_zones(self):
        from ffn_fe100_live_sessions import LiveSessions
        from ffn_fe100_sessions import key4,entry4
        from ffn_fe100_session_adapter import encode_native
        io=LiveSessions.__new__(LiveSessions);io.writable=True;io.endpoint=Mock()
        io.status=lambda:dict(blockers=['missing current training'],action_blockers=['missing SEM'],
                              warm_lab_verified=True,commissioning_blockers=[])
        wire=lambda zone:encode_native(entry4(key4('198.18.0.1','198.18.0.2',1234,2345,17,zone),1))
        io.commissioning=False
        with self.assertRaises(RuntimeError):io.call('insert',wire(4094))
        io.endpoint.call.assert_not_called();io.commissioning=True
        with self.assertRaises(RuntimeError):io.call('insert',wire(1))
        io.endpoint.call.assert_not_called();io.call('insert',wire(4094));io.endpoint.call.assert_called_once()


if __name__=='__main__':unittest.main()
