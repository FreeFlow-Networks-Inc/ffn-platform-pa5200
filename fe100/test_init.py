"""Boot-time FE100 initialisation: order, idempotence, lock retry, failure."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_fe100_init as I

BOOT = '11111111-2222-3333-4444-555555555555'


class FakeTools:
    """Readiness flips to initialised once every step has run."""
    def __init__(self, fail=None, contend=0):
        self.calls = []; self.fail = fail; self.contend = contend; self.done = set()
        self.names = [n for n, c in I.plan() if c is not None]

    def __call__(self, argv):
        self.calls.append(argv)
        if argv == ['ffn_fe100_ddr.py']:
            return 0, json.dumps({'before': {}}), ''
        if argv[0] == 'ffn_fe100_live_sessions.py':
            ready = self.done == set(self.names)
            return 0, json.dumps({'initialized': ready, 'blockers': [] if ready else ['fhm0 lacks successful calibration in this boot'], 'action_blockers': []}), ''
        name = next(n for n, c in I.plan() if c is not None and c == argv)
        if self.contend:
            self.contend -= 1
            return 1, '', 'Traceback\nBlockingIOError: [Errno 11] fcntl.flock\n'
        if name == self.fail:
            return 2, '', 'RuntimeError: refusing to retrain'
        self.done.add(name)
        return 0, '{"stage": "completed"}', ''


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.clock = [100.0]
        self.now = lambda: self.clock[0]

    def run_init(self, tools, **kw):
        kw.setdefault('archive', lambda run, root, boot: I.archive_generation(run, root, boot, reset=lambda r: True, clock=lambda: 7))
        return I.initialise(tools, root=self.root, boot=BOOT, links=lambda: True, now=self.now, log=lambda *_: None, **kw)

    def test_runs_the_commissioned_order_once_and_records_every_step(self):
        tools = FakeTools()
        journal = self.run_init(tools)
        self.assertEqual(journal['stage'], 'completed')
        self.assertEqual([s['name'] for s in journal['steps']], [n for n, _ in I.plan()])
        self.assertEqual(journal['steps'][17]['name'], 'archive-generation'); self.assertEqual(journal['steps'][17]['result']['reason'], 'no external-table journal')
        self.assertEqual(journal['steps'][0]['name'], 'block-tlu'); self.assertEqual(journal['steps'][-1]['name'], 'sem')
        self.assertTrue(journal['readiness']['initialized'])
        saved = json.loads((self.root / ('init-' + BOOT + '.json')).read_text())
        self.assertEqual(saved['stage'], 'completed')
        self.assertTrue((self.root / ('init-%s-00-block-tlu.log' % BOOT[:8])).exists())
        # A second run in the same boot touches no tool: the hardware reads initialised.
        again = FakeTools(); again.done = set(again.names)
        self.assertEqual(self.run_init(again)['stage'], 'already-initialized')
        self.assertEqual(again.calls, [['ffn_fe100_live_sessions.py']])

    def test_resumes_after_an_interruption_without_repeating_completed_steps(self):
        tools = FakeTools()
        plan = I.plan()
        partial = {'schema': 1, 'cp_boot_id': BOOT, 'stage': 'started', 'started_monotonic': 90.0,
                   'steps': [{'name': n, 'command': c, 'rc': 0} for n, c in plan[:17]]}
        (self.root / ('init-' + BOOT + '.json')).write_text(json.dumps(partial))
        tools.done = {n for n, _ in plan[:17]}
        journal = self.run_init(tools)
        self.assertEqual(journal['stage'], 'completed')
        executed = [c for c in tools.calls if c[0] != 'ffn_fe100_live_sessions.py']
        self.assertEqual(executed, [c for _, c in plan[17:] if c is not None])

    def test_previous_boot_external_tables_are_archived_only_once_memory_is_reset(self):
        journal = self.root / I.EXTERNAL_TABLES
        journal.write_text(json.dumps({'stage': 'configuration-verified', 'cp_boot_id': 'older-boot'}))
        with self.assertRaisesRegex(RuntimeError, 'recovery review'):
            I.archive_generation(FakeTools(), self.root, BOOT, reset=lambda r: False)
        self.assertTrue(journal.exists())
        result = I.archive_generation(FakeTools(), self.root, BOOT, reset=lambda r: True, clock=lambda: 42)
        self.assertFalse(journal.exists())
        archived = self.root / 'external-tables-archived-older-bo-42.json'
        self.assertEqual(result['archived'], str(archived)); self.assertEqual(json.loads(archived.read_text())['cp_boot_id'], 'older-boot')
        journal.write_text(json.dumps({'stage': 'configuration-verified', 'cp_boot_id': BOOT}))
        self.assertEqual(I.archive_generation(FakeTools(), self.root, BOOT, reset=lambda r: False)['reason'], 'journal belongs to this boot')
        self.assertTrue(journal.exists())

    def test_retained_memory_stops_the_sequence_before_ddr_training(self):
        (self.root / I.EXTERNAL_TABLES).write_text(json.dumps({'stage': 'configuration-verified', 'cp_boot_id': 'older-boot'}))
        tools = FakeTools()
        with self.assertRaisesRegex(RuntimeError, 'recovery review'):
            self.run_init(tools, archive=lambda run, root, boot: I.archive_generation(run, root, boot, reset=lambda r: False))
        names = [s['name'] for s in json.loads((self.root / ('init-' + BOOT + '.json')).read_text())['steps']]
        self.assertEqual(names[-1], 'tcam-clocks'); self.assertNotIn('ddr-clocks', names)

    def test_failed_step_stops_the_sequence_and_is_not_retried_in_that_boot(self):
        tools = FakeTools(fail='fdt-train-6')
        with self.assertRaisesRegex(RuntimeError, 'fdt-train-6 failed'):
            self.run_init(tools)
        journal = json.loads((self.root / ('init-' + BOOT + '.json')).read_text())
        self.assertEqual(journal['stage'], 'failed'); self.assertEqual(journal['steps'][-1]['name'], 'fdt-train-6')
        self.assertNotIn('fcm-clocks', [s['name'] for s in journal['steps']])
        later = FakeTools()
        with self.assertRaisesRegex(RuntimeError, 'failed earlier in this boot'):
            self.run_init(later)
        self.assertEqual(later.calls, [['ffn_fe100_live_sessions.py']])

    def test_table_lock_contention_is_retried_but_other_errors_are_not(self):
        sleeps = []
        runner = I.Runner(sleep=sleeps.append)
        outcomes = iter([(1, '', 'BlockingIOError: fcntl.flock'), (1, '', 'BlockingIOError: fcntl.flock'), (0, 'ok', '')])
        class P:
            def __init__(self, rc, out, err): self.returncode, self.stdout, self.stderr = rc, out, err
        I.subprocess.run = lambda *a, **k: P(*next(outcomes))
        try:
            self.assertEqual(runner(['ffn_fe100_fcm.py', '--clocks']), (0, 'ok', ''))
            self.assertEqual(sleeps, [0.5, 0.5])
            outcomes = iter([(2, '', 'RuntimeError: refusing to retrain initialized FCM')])
            self.assertEqual(runner(['ffn_fe100_fcm.py', '--train', '0'])[0], 2)
        finally:
            import subprocess; I.subprocess = subprocess

    def test_links_must_be_active_and_other_boot_journals_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'links'):
            I.initialise(FakeTools(), root=self.root, boot=BOOT, links=lambda: False, now=self.now, log=lambda *_: None)
        (self.root / ('init-' + BOOT + '.json')).write_text(json.dumps({'cp_boot_id': 'other', 'steps': []}))
        with self.assertRaisesRegex(RuntimeError, 'another boot'):
            self.run_init(FakeTools())


if __name__ == '__main__':
    unittest.main()
