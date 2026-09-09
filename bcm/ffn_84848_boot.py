#!/usr/bin/env python3
"""Load RAM firmware on an identified BCM84848 using the CP's locked MDIO bus.

Register protocol: MDIO2ARM 32-bit transfers; little-endian ARM firmware.
This targets the measured 600d:84f9 revision and MDIO boot strap only.
No EEPROM or flash is programmed. Firmware is supplied by the owner.
"""
import argparse
import fcntl
import hashlib
import pathlib
import struct
import time
from ffn_mdio import Mdio

class Copper:
    def __init__(self, bus, phy):
        self.bus, self.phy = bus, phy
    def read(self, dev, reg):
        return self.bus.transfer(self.phy, dev, reg)
    def write(self, dev, reg, value):
        self.bus.transfer(self.phy, dev, reg, value)
    def handshake(self):
        end = time.monotonic() + 2
        time.sleep(0.001)
        while self.read(30, 0x400e) & 2:
            if time.monotonic() >= end: raise RuntimeError('PHY firmware busy')
            time.sleep(0.001)
    def enable(self):
        if not self.read(30, 0x400f) or self.read(1, 0) & 0x8000:
            raise RuntimeError('cannot enable a PHY without running firmware')
        self.handshake()
        # Clear super-isolate, fiber preference, and copper disable only.
        self.write(30, 0x401a, self.read(30, 0x401a) & ~0x8180)
        self.handshake()
        self.write(7, 0, self.read(7, 0) | 0x1200)
        self.handshake()
        print('PHY %d copper enabled, autonegotiation restarted' % self.phy, flush=True)
    def wait_arm(self):
        end = time.monotonic() + 1
        while not self.read(1, 0xa818) & 1:
            if time.monotonic() >= end: raise RuntimeError('MDIO2ARM timeout')
    def arm_write(self, address, value):
        self.write(1, 0xa817, 0)
        self.write(1, 0xa819, address & 65535)
        self.write(1, 0xa81a, address >> 16)
        self.write(1, 0xa81b, value & 65535)
        self.write(1, 0xa81c, value >> 16)
        self.write(1, 0xa817, 9)
        self.wait_arm()
    def arm_read(self, address):
        self.write(1, 0xa817, 0)
        self.write(1, 0xa819, address & 65535)
        self.write(1, 0xa81a, address >> 16)
        self.write(1, 0xa817, 10)
        self.wait_arm()
        return (self.read(1, 0xa81c) << 16) | self.read(1, 0xa81b)
    def boot(self, firmware):
        ident = (self.read(1, 2), self.read(1, 3))
        if ident != (0x600d, 0x84f9): raise RuntimeError('unexpected PHY: %r' % (ident,))
        version = self.read(30, 0x400f)
        if version and not self.read(1, 0) & 0x8000:
            print('PHY %d firmware already running: 0x%04x' % (self.phy, version), flush=True)
            return
        if self.read(30, 0x401a) & 0x2000:
            raise RuntimeError('SPI boot strap: refusing this MDIO-only sequence')
        # Put the embedded ARM into a boot-ROM loop while its RAM is filled.
        self.write(30, 0x4181, 0x017c)
        self.write(30, 0x4186, 0x8000)
        self.write(30, 0x4181, 0x0040)
        self.arm_write(0xc3000000, 0x001f)
        self.arm_write(0xffff0000, 0xeafffffe)
        self.write(30, 0x4181, 0)
        self.write(1, 0xa817, 0x39)
        self.write(1, 0xa81a, 0)
        self.write(1, 0xa819, 0)
        for offset in range(0, len(firmware), 4):
            value = struct.unpack_from('<I', firmware, offset)[0]
            self.write(1, 0xa81c, value >> 16)
            self.write(1, 0xa81b, value & 65535)
            if offset % 32768 == 0:
                print('PHY %d RAM %d/%d' % (self.phy, offset, len(firmware)), flush=True)
        self.write(1, 0xa817, 0)
        # Sample the whole address span before allowing execution.
        for offset in sorted(set(range(0, len(firmware), 4096)) | {len(firmware)-4}):
            if self.arm_read(offset) != struct.unpack_from('<I', firmware, offset)[0]:
                raise RuntimeError('firmware readback mismatch at 0x%x' % offset)
        self.arm_write(0xc3000000, 0x002c)
        self.write(1, 0, self.read(1, 0) | 0x8000)
        end = time.monotonic() + 15
        while True:
            version = self.read(30, 0x400f)
            if version and not self.read(1, 0) & 0x8000: break
            if time.monotonic() >= end: raise RuntimeError('firmware did not start')
            time.sleep(0.1)
        print('PHY %d firmware booted: 0x%04x' % (self.phy, version), flush=True)

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('firmware')
    p.add_argument('--phy', type=int, choices=range(16, 20), required=True)
    p.add_argument('--enable', action='store_true')
    args = p.parse_args()
    fw = pathlib.Path(args.firmware).read_bytes()
    if len(fw) != 200204: raise SystemExit('unexpected BCM84844 firmware length')
    if hashlib.sha256(fw).hexdigest() != '27b205a463460ead659e7b7bec59e0c025081bdf8afbd73c66e7bac5308d59a0':
        raise SystemExit('firmware differs from the validated BCM84848 image')
    print('Firmware SHA256 ' + hashlib.sha256(fw).hexdigest(), flush=True)
    # Prevent two cooperating loaders from interleaving MDIO2ARM sequences.
    with open('/run/lock/ffn-copper.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bus = Mdio()
        try:
            phy = Copper(bus, args.phy)
            phy.boot(fw)
            if args.enable:
                phy.enable()
        finally:
            bus.close()
