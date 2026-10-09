import json
from pathlib import Path
import tempfile
import types
import unittest

import ffn_tap_steering as steering


class SteeringCpus(unittest.TestCase):
    def test_upper_half_of_the_eligible_cpus_less_reservations_and_cpu_zero(self):
        # The PA-5220 dataplane: 40 cores, four owners holding CPUs 1-8.
        cpus = steering.steering_cpus(range(40), {1, 2, 3, 4, 5, 6, 7, 8})
        self.assertEqual(cpus, list(range(24, 40)))
        self.assertNotIn(0, steering.steering_cpus(range(40)))
        self.assertEqual(steering.steering_cpus(range(40))[0], 20)

    def test_small_machines_are_left_unsteered(self):
        self.assertEqual(steering.steering_cpus(range(8)), [])          # 7 eligible
        self.assertEqual(steering.steering_cpus(range(9)), [5, 6, 7, 8])  # 8 eligible
        self.assertEqual(steering.steering_cpus(range(12), set(range(1, 6))), [])

    def test_mask_is_the_kernel_bitmap_text(self):
        self.assertEqual(steering.mask([]), '00000000')
        self.assertEqual(steering.mask([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]), '03fffc00')
        self.assertEqual(steering.mask(range(24, 40)), '000000ff,ff000000')
        self.assertEqual(steering.mask([63]), '80000000,00000000')
        with self.assertRaises(ValueError):
            steering.mask(['3'])
        with self.assertRaises(ValueError):
            steering.mask([4096])

    def test_reserved_cpus_come_from_every_record_live_or_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.json').write_text(json.dumps(dict(pid=1, cpus=[1, 2])))
            (root / 'b.json').write_text(json.dumps(dict(pid=99999999, cpus=[5, 6])))
            (root / 'c.json').write_text('not json')
            (root / 'd.json').write_text(json.dumps(dict(cpus=['x', -1, 9])))
            (root / 'lock').write_text('')
            self.assertEqual(steering.reserved_cpus(root), {1, 2, 5, 6, 9})
            self.assertEqual(steering.reserved_cpus(root / 'missing'), set())


class Apply(unittest.TestCase):
    def runner(self, returncode=0, stderr=''):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=returncode, stdout='', stderr=stderr)
        return calls, run

    def test_mask_is_written_to_every_receive_queue_inside_the_namespace(self):
        calls, run = self.runner()
        self.assertEqual(steering.apply('ae1', range(24, 40), run=run), '000000ff,ff000000')
        (argv, kwargs), = calls
        self.assertEqual(argv[:6], ['ip', 'netns', 'exec', 'ffn-data', 'sh', '-c'])
        self.assertIn('/sys/class/net/ae1/queues/rx-*', argv[6])
        self.assertIn('echo 000000ff,ff000000 > "$q/rps_cpus" || exit 1', argv[6])
        self.assertEqual(kwargs['timeout'], 10)

    def test_device_and_namespace_names_are_validated_before_any_shell(self):
        calls, run = self.runner()
        for device in ('p1; reboot', 'eth0', '', None, 'ae0', 'p0'):
            with self.assertRaises(ValueError):
                steering.apply(device, [9], run=run)
        with self.assertRaises(ValueError):
            steering.apply('p1', [9], namespace='ffn data', run=run)
        self.assertEqual(calls, [])

    def test_a_failed_write_is_an_error_the_caller_can_report(self):
        calls, run = self.runner(returncode=1, stderr='sh: 1: cannot create /sys/...: Permission denied')
        with self.assertRaisesRegex(OSError, 'receive steering for p5 failed: sh: 1'):
            steering.apply('p5', [9], run=run)

    def test_steer_clears_the_mask_on_a_small_machine_and_reports_the_cpus(self):
        calls, run = self.runner()
        self.assertEqual(steering.steer('p1', allowed=range(4), reserved=set(), run=run), [])
        self.assertIn('echo 00000000 >', calls[0][0][6])
        cpus = steering.steer('p1', allowed=range(40), reserved={1, 2, 3, 4, 5, 6, 7, 8}, run=run)
        self.assertEqual(cpus, list(range(24, 40)))
        self.assertIn('echo 000000ff,ff000000 >', calls[1][0][6])


if __name__ == '__main__':
    unittest.main()
