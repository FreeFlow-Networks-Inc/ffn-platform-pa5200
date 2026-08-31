#!/usr/bin/env python3
"""ffn-cpdp -- control-plane side of the CP<->DP transport over PCIe.

Lets the x86 control plane program the OCTEON dataplane: read and write any
physical address on the DP, drive the FE100 (BCM88375) with the byte-swap
applied for you, and bring the BGX ports up or down.

Both halves reach a shared ring pair in OCTEON DRAM through paths already proven
on this hardware -- the SLI/BAR window here (ffn_octdram, byte-exact over 20 MB
with a matching sha256) and mmap of /dev/mem on the DP side. No interrupts, and
none of PAN's PCIC ring format: this is FFN's own protocol, so it can ship.

Every field in shared memory is BIG-ENDIAN. The OCTEON is big-endian and x86 is
little-endian, so one defined order is required or both sides read garbage;
big-endian keeps the DP side free of byte swapping. Each message also carries a
CRC32 of its payload, so a cache-coherency mistake is detected rather than
silently acted on.

usage:
  ffn_cpdp.py ping
  ffn_cpdp.py info
  ffn_cpdp.py memrd <addr> [--width 8|16|32|64] [--count N]
  ffn_cpdp.py memwr <addr> <value> [--width N]
  ffn_cpdp.py fe100rd <bar2-offset> [--count N]
  ffn_cpdp.py fe100wr <bar2-offset> <value>
  ffn_cpdp.py link [<port>] [--up | --down]
  ffn_cpdp.py led [<unit>]
"""
import argparse
import struct
import sys
import time
import zlib

sys.path.insert(0, "/opt/ffn-ngfw-v2")
sys.path.insert(0, "/opt/ffn-ngfw-v2/tools")
import ffn_octdram as od

PCI = "0000:01:00.0"

BASE = 0x28000000
SIZE = 0x00100000
MAGIC = 0x46464E4350445031          # "FFNCPDP1"
VERSION = 1
SLOT = 4096
NSLOTS = 32
CP2DP = 0x1000
DP2CP = 0x40000
HDR = 48                            # payload offset inside a slot
MAXPAY = SLOT - HDR

OP_PING, OP_INFO = 1, 2
OP_MEM_RD, OP_MEM_WR = 3, 4
OP_FE100_RD, OP_FE100_WR = 5, 6
OP_LINK_GET, OP_LINK_SET = 7, 8
OP_LED_GET = 9

# Block memory moves and PCI config access -- the primitives a DP boot
# needs. MEM_WR moves one value per message, which is ~150k round trips for
# a 1.2 MB bootloader; the block ops move a full 4048-byte payload instead.
OP_MEM_WRBLK = 15
OP_MEM_RDBLK = 16
OP_PCI_CFG_RD = 17
OP_PCI_CFG_WR = 18
BLK_MAX = 4048          # FFN_CPDP_SLOT - FFN_CPDP_HDR

FE100_BAR2 = 0x11C0100800000
LEDUP0_CTRL = 0x20000
LEDUP0_CLK_DIV = 0x2005C

ST = {0: "OK", 1: "BADOP", 2: "BADARG", 3: "BADCRC", 4: "MAPFAIL",
      5: "IOFAIL", 6: "TOOBIG"}


class Transport(object):
    """One open SLI window, reused for the whole session."""

    def __init__(self, pci=PCI):
        self.w = od.WindowedDram(pci)
        self.dram = None
        self.seq = 0
        self.tx = 0                 # our producer index on CP->DP
        self.rx = 0                 # our consumer index on DP->CP

    def __enter__(self):
        self.dram = self.w.__enter__()
        return self

    def __exit__(self, *a):
        return self.w.__exit__(*a)

    # ---- raw region access ------------------------------------------------
    def rd(self, off, n):
        return self.w.read(BASE + off, n)

    def wr(self, off, data):
        self.w.write(BASE + off, data)

    def rd32(self, off):
        return struct.unpack(">I", self.rd(off, 4))[0]

    def wr32(self, off, v):
        self.wr(off, struct.pack(">I", v & 0xFFFFFFFF))

    def rd64(self, off):
        return struct.unpack(">Q", self.rd(off, 8))[0]

    # ---- handshake --------------------------------------------------------
    def ready(self):
        """True once the DP daemon has published the superblock magic."""
        try:
            return self.rd64(0) == MAGIC
        except Exception:
            return False

    def wait_ready(self, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.ready():
                # resync to whatever indices the daemon currently has, so a
                # restart of THIS tool does not desynchronise the rings
                self.tx = self.rd32(CP2DP + 0)
                self.rx = self.rd32(DP2CP + 0)
                return True
            time.sleep(0.2)
        return False

    def alive(self):
        return self.rd64(40)

    # ---- request / response ----------------------------------------------
    @staticmethod
    def _slot(ring_off, idx):
        return ring_off + 128 + (idx % NSLOTS) * SLOT

    def call(self, op, a0=0, a1=0, a2=0, payload=b"", timeout=5.0):
        if len(payload) > MAXPAY:
            raise ValueError("payload %d > %d" % (len(payload), MAXPAY))
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        crc = zlib.crc32(payload) & 0xFFFFFFFF if payload else 0
        hdr = struct.pack(">IHHII", self.seq, op, 0, len(payload), crc)
        hdr += struct.pack(">QQQ", a0, a1, a2)
        hdr += b"\x00" * (HDR - len(hdr))

        self.wr(self._slot(CP2DP, self.tx), hdr + payload)
        self.tx = (self.tx + 1) & 0xFFFFFFFF
        self.wr32(CP2DP + 0, self.tx)           # doorbell: bump head

        end = time.time() + timeout
        while time.time() < end:
            if self.rd32(DP2CP + 0) != self.rx:
                off = self._slot(DP2CP, self.rx)
                raw = self.rd(off, HDR)
                rseq, rop, rst, rlen, rcrc = struct.unpack(">IHHII", raw[:16])
                r0, r1, r2 = struct.unpack(">QQQ", raw[16:40])
                body = self.rd(off + HDR, rlen) if rlen else b""
                self.rx = (self.rx + 1) & 0xFFFFFFFF
                self.wr32(DP2CP + 4, self.rx)   # release the slot
                if rseq != self.seq:
                    raise IOError("sequence mismatch: sent %d got %d"
                                  % (self.seq, rseq))
                if rlen and (zlib.crc32(body) & 0xFFFFFFFF) != rcrc:
                    raise IOError("payload CRC mismatch from DP")
                return rst, r1, r2, body
            time.sleep(0.002)
        raise IOError("DP did not answer op %d within %.1fs" % (op, timeout))


def need_ok(st, what):
    if st != 0:
        print("  %s failed: %s" % (what, ST.get(st, st)))
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ping")
    sub.add_parser("info")
    p = sub.add_parser("memrd")
    p.add_argument("addr")
    p.add_argument("--width", type=int, default=32)
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("memwr")
    p.add_argument("addr")
    p.add_argument("value")
    p.add_argument("--width", type=int, default=32)
    p = sub.add_parser("fe100rd")
    p.add_argument("off")
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("fe100wr")
    p.add_argument("off")
    p.add_argument("value")
    p = sub.add_parser("link")
    p.add_argument("port", nargs="?", type=int, default=None)
    p.add_argument("--up", action="store_true")
    p.add_argument("--down", action="store_true")
    p = sub.add_parser("led")
    p.add_argument("unit", nargs="?", type=int, default=0)
    p = sub.add_parser('memwrblk', help='stage a file into Octeon memory')
    p.add_argument('addr')
    p.add_argument('file')
    p = sub.add_parser('memrdblk', help='read a block of Octeon memory')
    p.add_argument('addr')
    p.add_argument('len')
    p.add_argument('--out')
    p = sub.add_parser('cfgrd', help='PCI config read on the CP bus')
    p.add_argument('path')
    p.add_argument('off')
    p.add_argument('--width', type=int, default=32)
    p = sub.add_parser('cfgwr', help='PCI config write on the CP bus')
    p.add_argument('path')
    p.add_argument('off')
    p.add_argument('value')
    p.add_argument('--width', type=int, default=32)
    
    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return 2

    with Transport() as t:
        if not t.wait_ready():
            print("DP transport not ready at 0x%x (magic not published)." % BASE)
            print("Start the daemon on the OCTEON:  /bin/ffn_cpdpd &")
            return 2

        if a.cmd == "ping":
            st, r1, r2, _ = t.call(OP_PING)
            if not need_ok(st, "ping"):
                return 1
            print("  DP alive. version=%d magic=%s poll_count=%d"
                  % (r1, "OK" if r2 == MAGIC else "0x%x MISMATCH" % r2,
                     t.alive()))
        elif a.cmd == "info":
            st, r1, r2, body = t.call(OP_INFO)
            if not need_ok(st, "info"):
                return 1
            print("  cores : %d" % r1)
            print("  kernel: %s" % body.decode("ascii", "replace"))
        elif a.cmd == "memrd":
            addr = int(a.addr, 0)
            st, _, _, body = t.call(OP_MEM_RD, addr, a.width, a.count)
            if not need_ok(st, "memrd"):
                return 1
            w = a.width // 4
            for i in range(len(body) // 8):
                v = struct.unpack(">Q", body[i * 8:i * 8 + 8])[0]
                print("  0x%011x = 0x%0*x"
                      % (addr + i * (a.width // 8), w, v))
        elif a.cmd == "memwr":
            st, _, _, _ = t.call(OP_MEM_WR, int(a.addr, 0), a.width,
                                 int(a.value, 0))
            if not need_ok(st, "memwr"):
                return 1
            print("  wrote 0x%x -> 0x%x (%d-bit)"
                  % (int(a.value, 0), int(a.addr, 0), a.width))
        elif a.cmd == "fe100rd":
            off = int(a.off, 0)
            st, _, _, body = t.call(OP_FE100_RD, off, a.count)
            if not need_ok(st, "fe100rd"):
                return 1
            for i in range(len(body) // 4):
                v = struct.unpack(">I", body[i * 4:i * 4 + 4])[0]
                print("  BAR2+0x%05x = 0x%08x  (byte-swapped for you)"
                      % (off + i * 4, v))
        elif a.cmd == "fe100wr":
            st, _, _, _ = t.call(OP_FE100_WR, int(a.off, 0), int(a.value, 0))
            if not need_ok(st, "fe100wr"):
                return 1
            print("  wrote 0x%08x -> BAR2+0x%05x"
                  % (int(a.value, 0), int(a.off, 0)))
        elif a.cmd == "link":
            ports = [a.port] if a.port is not None else [0, 1]
            for pt in ports:
                if a.up or a.down:
                    st, fl, car, _ = t.call(OP_LINK_SET, pt, 1 if a.up else 0)
                else:
                    st, fl, car, _ = t.call(OP_LINK_GET, pt)
                if not need_ok(st, "link eth%d" % pt):
                    continue
                print("  eth%d  flags=0x%04x up=%s carrier=%s"
                      % (pt, fl, "yes" if fl & 1 else "no",
                         "?" if car > 1 else car))
        elif a.cmd == 'memwrblk':
            addr = int(a.addr, 0)
            data = open(a.file, 'rb').read()
            sent = 0
            # One message per 4048-byte chunk. The daemon maps a 64 KB window per
            # chunk, so a chunk that straddles a boundary costs two mappings.
            while sent < len(data):
                chunk = data[sent:sent + BLK_MAX]
                st, _, _, _ = t.call(OP_MEM_WRBLK, addr + sent, len(chunk),
                                     payload=chunk)
                if st:
                    print('  failed at +0x%x: %s' % (sent, ST.get(st, st)))
                    return 1
                sent += len(chunk)
            print('  wrote %d bytes to 0x%x in %d messages'
                  % (sent, addr, (sent + BLK_MAX - 1) // BLK_MAX))
        elif a.cmd == 'memrdblk':
            addr, n = int(a.addr, 0), int(a.len, 0)
            buf = b''
            while len(buf) < n:
                want = min(BLK_MAX, n - len(buf))
                st, _, _, body = t.call(OP_MEM_RDBLK, addr + len(buf), want)
                if st:
                    print('  failed at +0x%x: %s' % (len(buf), ST.get(st, st)))
                    return 1
                buf += bytes(body[:want])
            if a.out:
                open(a.out, 'wb').write(buf)
                print('  read %d bytes from 0x%x -> %s' % (len(buf), addr, a.out))
            else:
                for i in range(0, len(buf), 16):
                    print('  %013x  %s' % (addr + i, buf[i:i + 16].hex(' ')))
        elif a.cmd == 'cfgrd':
            st, v, _, _ = t.call(OP_PCI_CFG_RD, int(a.off, 0), a.width,
                                 payload=a.path.encode())
            if st:
                print('  failed: %s' % ST.get(st, st))
                return 1
            print('  %s +0x%02x (%d-bit) = 0x%x' % (a.path, int(a.off, 0),
                                                    a.width, v))
        elif a.cmd == 'cfgwr':
            st, _, _, _ = t.call(OP_PCI_CFG_WR, int(a.off, 0), a.width,
                                 int(a.value, 0), payload=a.path.encode())
            if st:
                print('  failed: %s' % ST.get(st, st))
                return 1
            st, v, _, _ = t.call(OP_PCI_CFG_RD, int(a.off, 0), a.width,
                                 payload=a.path.encode())
            print('  wrote 0x%x -> +0x%02x; reads back 0x%x'
                  % (int(a.value, 0), int(a.off, 0), v))
        elif a.cmd == "led":
            st, ctrl, div, _ = t.call(OP_LED_GET, a.unit)
            if not need_ok(st, "led"):
                return 1
            print("  LEDUP%d CTRL   = 0x%08x  (%s)"
                  % (a.unit, ctrl, "running" if ctrl else "STOPPED"))
            print("  LEDUP0 CLK_DIV= 0x%08x  (%d)" % (div, div))
    return 0


if __name__ == "__main__":
    sys.exit(main())
