import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import ffn_aggregate_hardware as hardware


class HeartbeatTests(unittest.TestCase):
    def auto_state(self):
        return dict(ports=[21,22],auto_speed={'21':dict(state='autoneg',supported=[100000,40000],remaining=[100000,40000],next_at=8)})

    def test_auto_trials_are_bounded_and_use_detected_capabilities(self):
        state=self.auto_state();physical={32:dict(port=32,enabled=True,link=False)}
        with tempfile.TemporaryDirectory() as tmp,patch.object(hardware,'FACEPLATE_LOCK',Path(tmp)/'lock'), \
                patch.object(hardware,'call',side_effect=lambda r:dict(ports=list(physical.values())) if r['op']=='port.list' else {}) as call:
            self.assertIsNone(hardware.detect_speeds(state,physical,7));call.assert_not_called()
            for now,speed in [(8,'100000'),(16,'40000'),(24,'auto')]:
                self.assertEqual(hardware.detect_speeds(state,physical,now),21)
                self.assertEqual(call.call_args.args[0]['speed'],speed)
            count=call.call_count
            self.assertIsNone(hardware.detect_speeds(state,physical,83));self.assertEqual(call.call_count,count)

    def test_auto_leaves_linked_and_explicit_members_untouched(self):
        state=self.auto_state();physical={32:dict(enabled=True,link=True,speed_mb=40000),33:dict(enabled=True,link=False)}
        with patch.object(hardware,'call') as call:
            self.assertIsNone(hardware.detect_speeds(state,physical,20));call.assert_not_called()
        self.assertEqual(state['auto_speed']['21']['selected_speed'],40000)
        self.assertNotIn('22',state['auto_speed'])

    def test_carrier_race_is_rechecked_before_speed_change(self):
        state=self.auto_state();physical={32:dict(enabled=True,link=False)}
        with tempfile.TemporaryDirectory() as tmp,patch.object(hardware,'FACEPLATE_LOCK',Path(tmp)/'lock'), \
                patch.object(hardware,'call',return_value={'ports':[dict(port=32,enabled=True,link=True)]}) as call:
            self.assertIsNone(hardware.detect_speeds(state,physical,8))
        self.assertEqual(call.call_count,1)

    def test_transient_lock_contention_retries_real_readback(self):
        observed={'phase':'active','links':[{'up':True}]}
        with patch.object(hardware,'execute',side_effect=[hardware.HardwareBusy('busy'),observed]) as execute,patch.object(hardware.time,'monotonic',return_value=0),patch.object(hardware.time,'sleep'):
            self.assertEqual(hardware.heartbeat({'token':'owner'},3.5),observed)
            self.assertEqual(execute.call_count,2)

    def test_lease_sdk_and_identity_failures_are_not_retried(self):
        for error in (RuntimeError('lease expired'),ValueError('owner changed'),OSError('SDK disconnected')):
            with patch.object(hardware,'execute',side_effect=error) as execute:
                with self.assertRaises(type(error)):hardware.heartbeat({})
                self.assertEqual(execute.call_count,1)

    def test_busy_cannot_extend_deadline_or_return_cached_success(self):
        with patch.object(hardware,'execute',side_effect=hardware.HardwareBusy('busy')) as execute,patch.object(hardware.time,'monotonic',side_effect=[0,1,2.5]),patch.object(hardware.time,'sleep'):
            with self.assertRaises(hardware.HardwareBusy):hardware.heartbeat({})
            self.assertEqual(execute.call_count,2)


if __name__=='__main__':unittest.main()
