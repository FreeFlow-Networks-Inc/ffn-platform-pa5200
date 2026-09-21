import unittest
from unittest.mock import patch
import ffn_aggregate_hardware as hardware


class HeartbeatTests(unittest.TestCase):
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
