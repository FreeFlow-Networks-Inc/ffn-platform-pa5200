import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_fe100_policy_control as control
import ffn_fe100_recovery as recovery
from ffn_fe100_journal import Journal


class Recovery(unittest.TestCase):
    def test_empty_recovery_is_idempotent_and_opens_no_hardware(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(control, 'ROOT', Path(directory)):
            first = control.control('replace', {'revision': 0, 'digest': 'a'*64})
            for _ in range(2):
                result = control.control('reconcile', {})
                self.assertEqual(result, first)
            self.assertFalse(result['admission_enabled'])

    def test_journal_lock_contention_does_not_report_drained(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(control, 'ROOT', Path(directory)):
            journal = Journal(Path(directory)/'policy-sessions.sqlite3')
            try: self.assertEqual(recovery.recover()['outcome'], 'busy')
            finally: journal.close()
            self.assertEqual(recovery.recover()['outcome'], 'drained')

    def test_failures_and_partial_drain_are_reported(self):
        def failed(*args): raise RuntimeError('ownership conflict')
        self.assertEqual(recovery.recover(failed)['outcome'], 'blocked')
        result = recovery.recover(lambda *args: {'phase': 'blocked', 'sessions': 1,
            'admission_enabled': False, 'recovery_required': False})
        self.assertEqual(result['outcome'], 'blocked')
        self.assertFalse(result['hardware_activation_verified'])

    def test_atomic_report_and_strict_control_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'report.json'
            recovery.publish({'outcome': 'blocked'}, path)
            self.assertEqual(json.loads(path.read_text()), {'outcome': 'blocked'})
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
        for operation, payload in [('reconcile', {'revision': 1}), ('activate', {}), ('status', [])]:
            with self.assertRaises(ValueError): control.control(operation, payload)


if __name__ == '__main__': unittest.main()
