"""Latched pipeline status: write-1-to-clear, configuration bits untouched, live faults reported."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_fe100_pipeline_errors as P


class FakeDevice:
    """cr.mode semantics: bits 12/13 clear when written with 1; everything else is read/write."""
    def __init__(self, values, stuck=()):
        self.values = dict(values); self.stuck = set(stuck); self.writes = []

    def read32(self, register):
        return self.values[register]

    def write32(self, register, value):
        self.writes.append((register, value))
        config = value & ~P.STATUS
        status = self.values[register] & P.STATUS & ~value if register not in self.stuck else self.values[register] & P.STATUS
        self.values[register] = config | status


class ClearTests(unittest.TestCase):
    def test_stale_fatal_status_is_cleared_and_configuration_kept(self):
        device = FakeDevice({0x40200: 0x4300, 0x48080: 0x4300, 0x70500: 0x5300})
        report = P.clear(device)
        self.assertEqual(report['cleared'], ['PRW']); self.assertEqual(report['persisting'], [])
        self.assertEqual(report['before']['PRW'], '0x5300'); self.assertEqual(report['after']['PRW'], '0x4300')
        self.assertEqual(device.writes, [(0x70500, 0x5300)])   # only the latched block is written

    def test_clean_pipelines_are_not_written(self):
        device = FakeDevice({r: 0x4300 for r in P.PIPELINES})
        report = P.clear(device)
        self.assertEqual(device.writes, []); self.assertEqual(report['cleared'], []); self.assertEqual(report['persisting'], [])

    def test_a_live_fault_persists_and_is_named(self):
        device = FakeDevice({0x40200: 0x6300, 0x48080: 0x4300, 0x70500: 0x5300}, stuck={0x70500})
        report = P.clear(device)
        self.assertEqual(report['cleared'], ['FLU']); self.assertEqual(report['persisting'], ['PRW'])

    def test_cli_exit_code_follows_persisting_faults(self):
        device = FakeDevice({0x40200: 0x4300, 0x48080: 0x4300, 0x70500: 0x5300}, stuck={0x70500})
        import types
        fake = types.SimpleNamespace(Fe100=lambda: device, memory_decode_on=lambda: True)
        device.close = lambda: None
        sys.modules['ffn_fe100'] = fake
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(P.main(['--clear']), 1)
            self.assertIn('"persisting": ["PRW"]', out.getvalue())
            device.values[0x70500] = 0x4300
            with redirect_stdout(io.StringIO()):
                self.assertEqual(P.main([]), 0)   # observe-only run, nothing written
        finally:
            del sys.modules['ffn_fe100']


if __name__ == '__main__':
    unittest.main()
