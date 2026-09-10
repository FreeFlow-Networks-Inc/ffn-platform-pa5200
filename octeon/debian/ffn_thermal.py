#!/usr/bin/env python3
"""PA-5220 CP thermal governor. Owner libthermal/libfans register references.

Uses kernel-owned mux channels, ADT7470 ID checks, 12 board/core sensors,
and eight tachometers. Unknown readings demand full PWM. No reset writes.
"""
import argparse
import ctypes
import fcntl
import json
import math
import mmap
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import time
from ffn_i2cread import read_regs, Msg, Ioctl, I2C_RDWR
from ffn_chassis_led import access as chassis_access

MUX = Path('/sys/bus/i2c/devices/1-0073')
SENSORS = [
    ('NB FE100', None, 0x48, 50, 70),
    ('NB Cavium', None, 0x49, 65, 85),
    ('NB front', None, 0x4a, 50, 70),
    ('NB BCM', None, 0x4b, 50, 70),
    ('CP core', None, 0x2a, 65, 85),
    ('BCM core', None, 0x1f, 70, 85),
    ('FE100 core', None, 0x1e, 50, 70),
    ('CE front', 0, 0x48, 50, 70),
    ('CE DP area', 0, 0x49, 50, 70),
    ('CE rear', 0, 0x4a, 50, 85),
    ('CE DP0/CE40', 0, 0x4b, 50, 75),
    ('DP0 core', 0, 0x1c, 70, 85),
]
BANKS = [(2, 0x2c), (3, 0x2e)]
MIN_PWM = 191  # Conservative commissioning floor: 75%, owner permits 105.


def bus(channel):
    return 0 if channel is None else int((MUX / ('channel-%d' % channel)).resolve(strict=True).name[4:])


def rd(channel, addr, reg):
    return read_regs(bus(channel), addr, reg, 1)[0]


def identify(channel, addr):
    if tuple(rd(channel, addr, r) for r in (0x3d, 0x3e, 0x3f)) != (0x70, 0x41, 2):
        raise RuntimeError('fan controller identity mismatch')


def force_full(enabled):
    # Owner ehmon_fan_control_init clears CPLD CSR17 bit6. Hardware testing
    # confirms its reset value forces both ADT7470 banks to full speed.
    with open('/run/ffn-chassis-led.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        try:
            page = mmap.PAGESIZE
            with mmap.mmap(fd, page, offset=0x1180000000000) as cfg:
                base = (struct.unpack_from('>Q', cfg, 0x10)[0] & 0xffff) << 16
            if base != 0x1b040000:
                raise RuntimeError('unrecognized CP CPLD mapping')
            with mmap.mmap(fd, page, offset=base) as csr:
                if csr[0] != 0x13:
                    raise RuntimeError('unrecognized CP CPLD version')
                value = (csr[0x11] & ~0x40) | (0x40 if enabled else 0)
                csr[0x11] = value
                if csr[0x11] != value:
                    raise RuntimeError('fan override readback failed')
        finally:
            os.close(fd)


def write_pwm(value):
    if type(value) is not int or not MIN_PWM <= value <= 255:
        raise ValueError('PWM outside commissioning range')
    force_full(value == 255)
    errors = []
    for channel, addr in BANKS:
        try:
            identify(channel, addr)
            fd = os.open('/dev/i2c-%d' % bus(channel), os.O_RDWR)
            try:
                for reg in range(0x32, 0x36):
                    data = (ctypes.c_uint8 * 2)(reg, value)
                    msgs = (Msg * 1)(Msg(addr, 0, 2, data))
                    fcntl.ioctl(fd, I2C_RDWR, Ioctl(msgs, 1))
                    if rd(channel, addr, reg) != value:
                        raise RuntimeError('PWM readback mismatch')
            finally:
                os.close(fd)
        except Exception as e:
            errors.append(str(e))
    if errors:
        raise RuntimeError('; '.join(errors))


def sample():
    result = {'temperatures': [], 'fans': [], 'errors': []}
    result.update(sample_power())
    try:
        mp = json.loads(Path('/run/ffn-mp-thermal.json').read_text())
        if not 0 <= time.time() - mp['received_at'] <= 45:
            raise ValueError('MP temperatures stale')
        validate_mp(mp['sensors'])
        result['mp_temperatures'] = mp['sensors']
    except Exception as e:
        result['errors'].append('MP thermal: %s' % e)
    for name, channel, addr, low, high in SENSORS:
        try:
            if addr >= 0x48:
                t = int.from_bytes(read_regs(bus(channel), addr, 0, 2), 'big', signed=True) / 256
            else:
                t = rd(channel, addr, 1)
            if not 0 < t < 125:
                raise ValueError('invalid temperature %s' % t)
            result['temperatures'].append({'name': name, 'celsius': t, 'ramp_start': low, 'maximum': high})
        except Exception as e:
            result['errors'].append('%s: %s' % (name, e))
    for channel, addr in BANKS:
        try:
            identify(channel, addr)
            # ADT7470 does not implement a sequential register block read.
            for i in range(4):
                reg = 0x2a + i * 2
                count = rd(channel, addr, reg) | rd(channel, addr, reg + 1) << 8
                rpm = 0 if count in (0, 65535) else round(5400000 / count)
                result['fans'].append({'bank': 4 - channel, 'fan': i + 1, 'rpm': rpm,
                                       'pwm': rd(channel, addr, 0x32 + i)})
                if rpm < 5000 or rpm > 20000:
                    result['errors'].append('fan tachometer outside valid range')
        except Exception as e:
            result['errors'].append('fan bank %s: %s' % (4 - channel, e))
    return result


def demand(s):
    if s['errors'] or len(s['temperatures']) != 12 or len(s['fans']) != 8 or not s.get('mp_temperatures'):
        return 255
    ratio = max((t['celsius'] - t['ramp_start']) / (t['maximum'] - t['ramp_start'])
                for t in s['temperatures'] + s['mp_temperatures'])
    return min(255, max(MIN_PWM, math.ceil(MIN_PWM + (255 - MIN_PWM) * ratio)))


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect('\0' + address[1:] if address.startswith('@') else address)
            sock.sendall(message.encode())


def sample_power():
    try:
        status = chassis_access()
        return {'power_csr': status['power_csr'],
                'power_supplies': status['power_supplies'], 'power_errors': []}
    except Exception as e:
        return {'power_supplies': [], 'power_errors': [str(e)]}


def led_policy(s):
    hot = any(t['celsius'] >= t['maximum'] for t in s['temperatures'] + s.get('mp_temperatures', []))
    bad = bool(s['errors'])
    supplies = {p['led']: p for p in s.get('power_supplies', [])}
    power_bad = bool(s.get('power_errors')) or set(supplies) != {'ps0', 'ps1'}
    updates = {}
    for led in ('ps0', 'ps1'):
        ps = supplies.get(led)
        healthy = ps and ps['present'] and ps['power_good']
        updates[led] = 'green' if healthy and not s.get('power_errors') else 'yellow'
        power_bad = power_bad or not healthy
    updates.update(fans='yellow' if bad else 'green',
                   temp='yellow' if bad or hot else 'green',
                   alarm='yellow' if bad or hot or power_bad else 'off')
    return updates


def leds(s):
    chassis_access(led_policy(s))


def validate_mp(sensors):
    if not isinstance(sensors, list) or not 1 <= len(sensors) <= 256:
        raise ValueError('invalid MP sensors')
    for s in sensors:
        if not (isinstance(s['name'], str) and 0 < s['celsius'] < 125 and
                40 <= s['ramp_start'] < s['maximum'] <= 105):
            raise ValueError('invalid MP thermal reading')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('status', 'run', 'full', 'receive-mp'))
    a = p.parse_args()
    if a.action == 'receive-mp':
        sensors = json.load(sys.stdin)
        validate_mp(sensors)
        path = Path('/run/ffn-mp-thermal.json.tmp')
        path.write_text(json.dumps({'received_at': time.time(), 'sensors': sensors}))
        path.replace('/run/ffn-mp-thermal.json')
        return
    if a.action == 'status':
        print(json.dumps(sample(), indent=2))
        return
    with open('/run/ffn-thermal.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if a.action == 'full':
            write_pwm(255)
            leds(dict(temperatures=[], errors=['automatic monitoring stopped'], **sample_power()))
            return
        def stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        current = 255
        cool_since = time.monotonic()
        try:
            write_pwm(255)
            notify('READY=1')
            while True:
                s = sample()
                wanted = demand(s)
                now = time.monotonic()
                if wanted >= current:
                    current = wanted
                    cool_since = now
                elif now - cool_since >= 30:
                    current = max(wanted, current - 8)
                write_pwm(current)
                s.update(pwm_command=current, demand=wanted, timestamp=time.time())
                temp = Path('/run/ffn-thermal.json.tmp')
                temp.write_text(json.dumps(s, indent=2) + '\n')
                temp.replace('/run/ffn-thermal.json')
                leds(s)
                notify('WATCHDOG=1\nSTATUS=PWM %d/255; %d sensor errors' % (current, len(s['errors'])))
                time.sleep(5)
        finally:
            write_pwm(255)


if __name__ == '__main__':
    main()
