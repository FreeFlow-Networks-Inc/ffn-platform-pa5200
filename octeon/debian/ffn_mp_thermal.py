#!/usr/bin/env python3
"""Send MP core temperatures to the CP governor over the pinned management SSH."""
import json
from pathlib import Path
import subprocess
import time


def main():
    while True:
        try:
            sensors = []
            for h in Path('/sys/class/hwmon').glob('hwmon*'):
                if (h / 'name').read_text().strip() != 'coretemp':
                    continue
                for f in h.glob('temp*_input'):
                    stem = f.name[:-6]
                    maximum = float((h / (stem + '_max')).read_text()) / 1000
                    sensors.append({'name': 'MP ' + (h / (stem + '_label')).read_text().strip(),
                                    'celsius': float(f.read_text()) / 1000,
                                    'maximum': maximum, 'ramp_start': maximum - 20})
            if not sensors:
                raise RuntimeError('no MP core temperatures')
            subprocess.run(['/usr/local/sbin/ffn-cp',
                            'python3 /usr/local/sbin/ffn_thermal.py receive-mp'],
                           input=json.dumps(sensors).encode(), check=True, timeout=15)
        except Exception as e:
            print('MP thermal update failed:', e, flush=True)
        time.sleep(10)


if __name__ == '__main__':
    main()
