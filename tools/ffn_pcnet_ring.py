#!/usr/bin/env python3
"""ffn_pcnet ring protocol -- reference implementation and validator.

This is the wire format both endpoints implement (host in this module, the
OCTEON in C). Keeping a Python reference lets the whole protocol be proven before
any OCTEON binary is built or booted: the host accesses the region through the
index-1 BAR window, and a stand-in for the OCTEON accesses the SAME physical DRAM
through ffn_cpdp's memrdblk/memwrblk. Both must see identical bytes -- it is one
piece of DRAM -- so a round trip validates the format, the CRC, and the head/tail
discipline without a boot cycle.

Every multi-byte control field is big-endian, matching ffn_pcnet.h and ffn_cpdp:
the OCTEON is big-endian and reads them natively; here on x86 we pack/unpack with
'>'. Payload bytes are a byte stream and are not swapped.
"""
import struct
import zlib

# ---- constants, mirroring ffn_pcnet.h -------------------------------------
BASE = 0x29000000
SIZE = 0x00400000
MAGIC = 0x46464E504E455431           # "FFNPNET1"
VERSION = 1
H2O_OFF = 0x001000
O2H_OFF = 0x200000
NSLOTS = 256
SLOT = 2048
MTU = 1500
RING_HDR = 64
SLOT_HDR = 8                          # len(u32) + crc(u32)

# header field offsets (from BASE)
HDR = struct.Struct(">QIIIIIII")      # magic, version, nslots, slot_bytes,
                                      # h2o_off, o2h_off, host_up, oct_up


def slot_off(i):
    return RING_HDR + i * SLOT


class Ring:
    """One ring, addressed by a pair of (read, write) byte callables.

    read(off, n) -> bytes ; write(off, bytes). Offsets are relative to the ring
    base. The producer only ever advances head; the consumer only tail. head and
    tail live in the first 8 bytes so a consumer poll touches one line.
    """

    def __init__(self, base_off, read, write):
        self.b = base_off
        self.rd = read
        self.wr = write

    def _head(self):
        return struct.unpack(">I", self.rd(self.b + 0, 4))[0]

    def _tail(self):
        return struct.unpack(">I", self.rd(self.b + 4, 4))[0]

    def _set_head(self, v):
        self.wr(self.b + 0, struct.pack(">I", v))

    def _set_tail(self, v):
        self.wr(self.b + 4, struct.pack(">I", v))

    def put(self, frame):
        """Producer: enqueue one frame. Returns False if the ring is full."""
        if len(frame) > SLOT - SLOT_HDR:
            raise ValueError("frame %d > slot payload %d" % (len(frame), SLOT - SLOT_HDR))
        head = self._head()
        tail = self._tail()
        if (head + 1) % NSLOTS == tail:
            # ring full: bump the drop counter and decline
            dc = struct.unpack(">I", self.rd(self.b + 8, 4))[0]
            self.wr(self.b + 8, struct.pack(">I", (dc + 1) & 0xffffffff))
            return False
        off = self.b + slot_off(head)
        crc = zlib.crc32(frame) & 0xffffffff
        # payload and crc first, THEN len (len != 0 is the ready flag), THEN head
        self.wr(off + SLOT_HDR, frame)
        self.wr(off + 4, struct.pack(">I", crc))
        self.wr(off + 0, struct.pack(">I", len(frame)))
        self._set_head((head + 1) % NSLOTS)
        return True

    def get(self):
        """Consumer: dequeue one frame, or None. Raises on CRC mismatch."""
        head = self._head()
        tail = self._tail()
        if head == tail:
            return None
        off = self.b + slot_off(tail)
        ln = struct.unpack(">I", self.rd(off + 0, 4))[0]
        if ln == 0:
            return None                # producer advanced head but len not visible yet
        if ln > SLOT - SLOT_HDR:
            raise ValueError("slot %d len %d out of range" % (tail, ln))
        crc = struct.unpack(">I", self.rd(off + 4, 4))[0]
        data = self.rd(off + SLOT_HDR, ln)
        if zlib.crc32(data) & 0xffffffff != crc:
            raise ValueError("slot %d CRC mismatch" % tail)
        self.wr(off + 0, struct.pack(">I", 0))     # clear ready
        self._set_tail((tail + 1) % NSLOTS)
        return data


def read_hdr(read):
    return HDR.unpack(read(0, HDR.size))


# Field offsets within HDR: magic@0(8), version@8, nslots@0xc, slot@0x10,
# h2o_off@0x14, o2h_off@0x18, host_up@0x1c, oct_up@0x20. Naming them explicitly
# because writing the wrong one silently corrupts a neighbour -- an early bug
# wrote the up-flag at 0x18 and clobbered o2h_off to 1.
HOST_UP_OFF = 0x1c
OCT_UP_OFF = 0x20


def host_init(read, write):
    """Host owns region init: reset the header and both ring control blocks.

    The host is authoritative because it is up first and long-lived, while the
    OCTEON attaches and detaches across reboots. Resetting unconditionally wipes
    stale or half-written state from a previous OCTEON life rather than
    inheriting it -- which a conditional "write only if magic absent" would keep.
    oct_up is cleared here too, so a stale flag cannot look like a peer that has
    not actually attached yet.
    """
    write(0, HDR.pack(MAGIC, VERSION, NSLOTS, SLOT, H2O_OFF, O2H_OFF, 0, 0))
    for off in (H2O_OFF, O2H_OFF):
        write(off, b"\x00" * RING_HDR)
    write(HOST_UP_OFF, struct.pack(">I", 1))


def oct_attach(read, write):
    """OCTEON side: announce itself once the host's magic is present."""
    write(OCT_UP_OFF, struct.pack(">I", 1))
