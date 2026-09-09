import copy
import unittest
from ffn_thermal import demand, MIN_PWM, SENSORS


class ThermalPolicy(unittest.TestCase):
    def setUp(self):
        self.s = {'errors': [], 'fans': [{} for _ in range(8)],
                  'mp_temperatures': [{'celsius': 40, 'ramp_start': 62, 'maximum': 82}],
                  'temperatures': [{'celsius': low - 10, 'ramp_start': low, 'maximum': high}
                                   for _, _, _, low, high in SENSORS]}

    def test_cool_floor(self):
        self.assertEqual(demand(self.s), MIN_PWM)

    def test_each_sensor_can_demand_full(self):
        for i in range(12):
            s = copy.deepcopy(self.s)
            s['temperatures'][i]['celsius'] = s['temperatures'][i]['maximum']
            self.assertEqual(demand(s), 255)

    def test_sensor_error_or_missing_reading_demands_full(self):
        for field in ('temperatures', 'fans'):
            s = copy.deepcopy(self.s)
            s[field].pop()
            self.assertEqual(demand(s), 255)
        self.s['errors'].append('I2C timeout')
        self.assertEqual(demand(self.s), 255)

    def test_monotonic_ramp(self):
        values = []
        for t in range(40, 81):
            self.s['temperatures'][0]['celsius'] = t
            values.append(demand(self.s))
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 255)

    def test_mp_hot_or_missing_demands_full(self):
        self.s['mp_temperatures'][0]['celsius'] = 82
        self.assertEqual(demand(self.s), 255)
        self.s['mp_temperatures'] = []
        self.assertEqual(demand(self.s), 255)


if __name__ == '__main__':
    unittest.main()
