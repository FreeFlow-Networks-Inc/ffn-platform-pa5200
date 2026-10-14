import datetime
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import ffn_log_ship as ship


def export(*cursors):
    return b''.join(b'__CURSOR=' + c.encode() + b'\n__REALTIME_TIMESTAMP=1\nMESSAGE=x\n\n' for c in cursors)


def runner(stdout=b'', returncode=0, stderr=b'', calls=None):
    def run(cmd, capture_output, timeout):
        if calls is not None:
            calls.append(cmd)
        class Done:
            pass
        done = Done(); done.returncode = returncode; done.stdout = stdout; done.stderr = stderr
        return done
    return run


class LogShipTests(unittest.TestCase):
    def test_last_cursor_is_the_final_entry(self):
        self.assertEqual(ship.last_cursor(export('a', 'b', 'c')), 'c')
        self.assertIsNone(ship.last_cursor(b''))

    def test_pull_appends_to_the_day_file_and_advances_the_cursor(self):
        calls = []
        run = runner(export('c1', 'c2'), calls=calls)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = lambda: datetime.datetime(2026, 10, 14, 15, 0, tzinfo=datetime.timezone.utc)
            first = ship.pull('dp', root, run, clock)
            self.assertTrue(first['ok']); self.assertEqual(first['cursor'], 'c2')
            self.assertIn("'--since=-2h'", calls[0][-1])
            ship.pull('dp', root, run, clock)
            self.assertIn("'--after-cursor=c2'", calls[1][-1])
            self.assertEqual((root / 'dp' / '2026-10-14.export').read_bytes(), export('c1', 'c2') * 2)
            self.assertEqual((root / 'dp' / 'cursor').read_text().strip(), 'c2')

    def test_failure_keeps_the_cursor(self):
        run = runner(returncode=255, stderr=b'ssh: connect failed')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'cp').mkdir(); (root / 'cp' / 'cursor').write_text('keep\n')
            result = ship.pull('cp', root, run)
            self.assertFalse(result['ok']); self.assertIn('connect failed', result['error'])
            self.assertEqual((root / 'cp' / 'cursor').read_text().strip(), 'keep')

    def test_oversized_pull_never_splits_an_entry(self):
        run = runner(export(*['c%d' % i for i in range(5000)]))
        with tempfile.TemporaryDirectory() as tmp, patch.object(ship, 'MAX_BYTES', 1000):
            result = ship.pull('dp', Path(tmp), run)
            self.assertTrue(result['truncated']); self.assertTrue(result['cursor'].startswith('c'))
            data = ship.day_file(Path(tmp), 'dp').read_bytes()
            self.assertTrue(data.endswith(b'\n\n')); self.assertLessEqual(len(data), 1000)
            self.assertEqual(ship.last_cursor(data), result['cursor'])


if __name__ == '__main__':
    unittest.main()
