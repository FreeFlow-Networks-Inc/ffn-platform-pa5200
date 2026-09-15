import tempfile
import os
import unittest
from pathlib import Path
from ffn_fe100_journal import Journal
from ffn_fe100_sessions import SessionManager
import test_sessions as fixtures


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'sessions.db'
        fixture = fixtures.Sessions(); fixture.setUp()
        self.entries = fixture.entries
        self.backend = fixtures.Backend()
        self.journal = Journal(self.path)
        self.addCleanup(lambda: self.journal.close())

    def restart(self):
        self.journal.close()
        self.journal = Journal(self.path)
        return SessionManager(self.backend, self.journal)

    def test_restart_reclaims_both_directions_before_reuse(self):
        manager = SessionManager(self.backend, self.journal)
        manager.install(1, self.entries, 9)
        manager = self.restart()
        self.assertTrue(manager.recovery_required)
        with self.assertRaises(RuntimeError): manager.install(2, self.entries, 10)
        manager.recover()
        self.assertFalse(self.backend.rows)
        self.assertFalse(self.journal.load())
        manager.install(2, self.entries, 10)

    def test_crash_after_first_write(self):
        # Durable intent exists, but process died before the reverse insert.
        self.journal.put(1, {'entries':self.entries, 'revision':9, 'state':'installing'})
        self.backend.insert(self.entries[0])
        manager = self.restart()
        manager.recover()
        self.assertFalse(manager.sessions)
        self.assertFalse(self.backend.rows)

    def test_recovery_preserves_foreign_entry(self):
        manager = SessionManager(self.backend, self.journal)
        manager.install(1, self.entries, 9)
        foreign = self.entries[0][:36] + bytes.fromhex('12345678') + self.entries[0][40:]
        self.backend.rows[foreign[:16]] = foreign
        manager = self.restart()
        with self.assertRaises(RuntimeError): manager.recover()
        self.assertTrue(manager.recovery_required)
        self.assertEqual(self.backend.fetch(foreign[:16]), foreign)
        self.assertEqual(self.journal.load()[1]['state'], 'unknown')

    def test_intent_commit_failure_prevents_hardware_write(self):
        manager = SessionManager(self.backend, self.journal)
        self.journal.put = lambda *a: (_ for _ in ()).throw(OSError('disk full'))
        with self.assertRaises(OSError): manager.install(1, self.entries, 9)
        self.assertEqual(self.backend.writes, 0)
        self.assertTrue(manager.recovery_required)

    def test_second_owner_cannot_open_journal(self):
        with self.assertRaises(BlockingIOError): Journal(self.path)

    def test_process_exit_during_first_insert(self):
        self.journal.close()
        written = Path(self.temp.name)/'hardware-entry'
        pid = os.fork()
        if pid == 0:
            journal = Journal(self.path)
            def interrupted_insert(entry):
                written.write_text(entry.hex())
                os._exit(23)  # bypass exception handlers and Python cleanup
            self.backend.insert = interrupted_insert
            SessionManager(self.backend, journal).install(1, self.entries, 9)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.journal = Journal(self.path)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        entry = bytes.fromhex(written.read_text())
        self.backend.rows[entry[:16]] = entry
        manager = SessionManager(self.backend, self.journal)
        self.assertEqual(manager.sessions[1]['state'], 'installing')
        manager.recover()
        self.assertFalse(self.backend.rows)
        self.assertFalse(self.journal.load())


if __name__ == '__main__': unittest.main()
