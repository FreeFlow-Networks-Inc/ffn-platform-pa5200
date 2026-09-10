import unittest
from unittest.mock import patch

from ffn_chassis_led import decode_power
from ffn_thermal import led_policy, sample_power


class PowerPolicy(unittest.TestCase):
    def sample(self, csr=0x3c):
        return dict(temperatures=[], errors=[], power_errors=[],
                    power_supplies=decode_power(csr))

    def test_healthy(self):
        self.assertEqual(led_policy(self.sample()), dict(
            ps0='green', ps1='green', fans='green', temp='green', alarm='off'))

    def test_each_supply_fault_and_recovery(self):
        for csr, led, other in [(0x0c, 'ps0', 'ps1'), (0x30, 'ps1', 'ps0'),
                                (0x3e, 'ps0', 'ps1'), (0x3d, 'ps1', 'ps0')]:
            result = led_policy(self.sample(csr))
            self.assertEqual(result[led], 'yellow')
            self.assertEqual(result[other], 'green')
            self.assertEqual(result['alarm'], 'yellow')
            self.assertEqual(result['fans'], 'green')
        self.assertEqual(led_policy(self.sample())['alarm'], 'off')

    def test_owner_any_good_bit_and_presence_gate(self):
        for csr in (0x14, 0x18, 0x24, 0x28):
            self.assertTrue(all(p['power_good'] for p in decode_power(csr)))
        for p in decode_power(0x3f):
            self.assertFalse(p['power_good'])
            self.assertEqual(p['state'], 'absent')

    def test_unreadable_or_missing_is_unknown(self):
        with patch('ffn_thermal.chassis_access', side_effect=OSError('read failed')):
            power = sample_power()
        s = self.sample()
        s.update(power)
        result = led_policy(s)
        for led in ('ps0', 'ps1', 'alarm'):
            self.assertEqual(result[led], 'yellow')
        s['power_supplies'] = decode_power(0x3c)[:1]
        self.assertEqual(led_policy(s)['alarm'], 'yellow')

    def test_power_recovery_does_not_clear_thermal_alarm(self):
        s = self.sample()
        s['temperatures'] = [dict(celsius=85, maximum=80)]
        self.assertEqual(led_policy(s)['alarm'], 'yellow')
        s['temperatures'] = []
        s['errors'] = ['fan missing']
        self.assertEqual(led_policy(s)['alarm'], 'yellow')


if __name__ == '__main__':
    unittest.main()
