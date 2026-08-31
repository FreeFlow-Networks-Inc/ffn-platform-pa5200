#!/usr/bin/env python3
"""ffn-csr -- read/write OCTEON CSRs through FFN's own kernel driver.

Replaces shelling out to the vendor `oct-remote-csr` tool. That tool and this
driver both drive the same stateful BAR0 window (address halves, then data), so
they must not be used concurrently -- one owner at a time. Going through the
driver also removes a vendor binary from FFN's dependency set.

usage: ffn_csr.py <addr> [value]
"""
import fcntl
import struct
import sys

DEV = "/dev/ffn_pcic"


def _ioc(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


CSR_RD = _ioc(3, "F", 1, 16)     # _IOWR('F', 1, struct{u64,u64})
CSR_WR = _ioc(1, "F", 2, 16)     # _IOW ('F', 2, struct{u64,u64})


def available():
    try:
        open(DEV, "rb").close()
        return True
    except OSError:
        return False


def read64(addr):
    with open(DEV, "rb") as f:
        buf = bytearray(struct.pack("=QQ", addr, 0))
        fcntl.ioctl(f, CSR_RD, buf, True)
        return struct.unpack("=QQ", bytes(buf))[1]


def write64(addr, value):
    with open(DEV, "rb") as f:
        buf = struct.pack("=QQ", addr, value)
        fcntl.ioctl(f, CSR_WR, buf)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    addr = int(sys.argv[1], 0)
    if len(sys.argv) > 2:
        write64(addr, int(sys.argv[2], 0))
        print("  0x%013x <- 0x%016x  (readback 0x%016x)"
              % (addr, int(sys.argv[2], 0), read64(addr)))
    else:
        print("  0x%013x = 0x%016x" % (addr, read64(addr)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
