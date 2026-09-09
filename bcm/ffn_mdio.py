#!/usr/bin/env python3
"""CP MIPS64 n64: locked Clause 45 access via /dev/ffn-mdio."""
import argparse
import fcntl
import os
import struct

# Linux MIPS _IOWR has direction 6 in bits 29..31, 13 size bits.
XFER = (6 << 29) | (20 << 16) | (ord('M') << 8) | 1

class Mdio:
    def __init__(self, bus=0):
        if bus not in (0, 1): raise ValueError('bus must be 0 or 1')
        self.fd = os.open('/dev/ffn-mdio1' if bus else '/dev/ffn-mdio', os.O_RDWR)

    def close(self):
        os.close(self.fd)

    def transfer(self, phy, devad, reg, value=None):
        if not (0 <= phy <= 31 and 0 <= devad <= 31 and 0 <= reg <= 65535):
            raise ValueError('invalid MDIO address')
        if value is not None and not 0 <= value <= 65535:
            raise ValueError('MDIO value must fit 16 bits')
        data = bytearray(struct.pack('=IIIII', int(value is not None), phy, devad, reg, value or 0))
        fcntl.ioctl(self.fd, XFER, data, True)
        return struct.unpack('=IIIII', data)[4]

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phy', type=lambda v: int(v, 0))
    parser.add_argument('devad', type=lambda v: int(v, 0))
    parser.add_argument('reg', type=lambda v: int(v, 0))
    parser.add_argument('--write', type=lambda v: int(v, 0))
    parser.add_argument('--bus', type=int, choices=(0, 1), default=0)
    args = parser.parse_args()
    bus = Mdio(args.bus)
    try:
        print('0x%04x' % bus.transfer(args.phy, args.devad, args.reg, args.write))
    finally:
        bus.close()
