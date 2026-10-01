import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_fe100_policy_control as control
from ffn_fe100_controld import dispatch,drained


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_persistent_owner_serializes_and_restarts_blocked(self):
        with patch.object(control,'ROOT',self.root):
            owner=control.PolicyController()
            try:
                with self.assertRaises(BlockingIOError):control.PolicyController()
                state=owner.execute('replace',dict(revision=0,digest='a'*64))
                self.assertTrue(drained(state));self.assertEqual(state['revision'],1)
                self.assertEqual(owner.execute('reconcile',{}),state)
                with self.assertRaises(ValueError):owner.execute('replace',dict(revision=0,digest='a'*64))
            finally:owner.close()
            self.assertTrue(drained(control.control('reconcile',{})))
            self.assertEqual(control.control('status',{})['revision'],1)

    def test_service_requirement_never_falls_back_after_rpc_failure(self):
        with patch.object(control,'ROOT',self.root),patch.object(control,'control') as direct, \
             patch('ffn_fe100_control_socket.request',side_effect=TimeoutError('owner unavailable')):
            (self.root/'control-service-required').touch()
            with self.assertRaises(TimeoutError):control.dispatch('status',{})
            direct.assert_not_called()

    def test_strict_drain_and_envelope(self):
        good=dict(phase='blocked',sessions=0,recovery_required=False,admission_enabled=False)
        self.assertTrue(drained(good))
        for key,value in [('sessions',False),('sessions',1),('phase','active'),
                          ('admission_enabled',True),('recovery_required',True)]:
            self.assertFalse(drained(dict(good,**{key:value})))
        with patch.object(control,'ROOT',self.root):
            owner=control.PolicyController()
            try:
                with self.assertRaises(ValueError):dispatch(owner,dict(operation='activate'))
            finally:owner.close()


if __name__=='__main__':unittest.main()
