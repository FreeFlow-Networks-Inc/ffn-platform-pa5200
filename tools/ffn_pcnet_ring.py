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

TRUST. The rings live in OCTEON DRAM, so EVERY field this module reads -- head,
tail, len, crc, payload -- is writable by the peer, including the fields the
protocol says are "ours". Three rules follow, and every accessor below keeps
them:

  * a peer-writable value is read from shared memory exactly ONCE per
    operation, into a local, and only the local is used afterwards -- there
    is no second read that could see a different value (no TOCTOU);
  * an index is MASKED before it becomes an offset, so no value the peer can
    write turns into an access outside the ring;
  * a bad slot (len 0, len out of range, CRC mismatch) is CONSUMED -- len
    cleared, tail advanced -- before the error is reported, so one corrupt
    slot can never wedge the ring. The old behaviour raised first and left
    tail in place, which made the host retry the same slot forever.
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
MAXFRAME = SLOT - SLOT_HDR

# The mask is what makes a peer-written index harmless. It requires NSLOTS to be
# a power of two; ffn_pcnet.h enforces the same at compile time.
SLOT_MASK = NSLOTS - 1
assert NSLOTS & SLOT_MASK == 0, "NSLOTS must be a power of two"

# header field offsets (from BASE)
HDR = struct.Struct(">QIIIIIII")      # magic, version, nslots, slot_bytes,
                                      # h2o_off, o2h_off, host_up, oct_up


def slot_off(i):
    """Byte offset of slot i within a ring. i is masked: head and tail come
    from peer-writable DRAM, and a value >= NSLOTS must never become an offset
    past the ring."""
    return RING_HDR + (i & SLOT_MASK) * SLOT


class RingError(ValueError):
    """A slot was rejected. It has already been consumed by the time this is
    raised, so the caller just counts it and carries on."""


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

    # head and tail are read ONCE per operation and masked on the way in. They
    # are stored modulo NSLOTS by both sides, so masking changes nothing for a
    # well-behaved peer and bounds everything for a hostile one.
    def _head(self):
        return struct.unpack(">I", self.rd(self.b + 0, 4))[0] & SLOT_MASK

    def _tail(self):
        return struct.unpack(">I", self.rd(self.b + 4, 4))[0] & SLOT_MASK

    def _set_head(self, v):
        self.wr(self.b + 0, struct.pack(">I", v & SLOT_MASK))

    def _set_tail(self, v):
        self.wr(self.b + 4, struct.pack(">I", v & SLOT_MASK))

    def put(self, frame):
        """Producer: enqueue one frame. Returns False if the ring is full."""
        if len(frame) > MAXFRAME:
            raise ValueError("frame %d > slot payload %d" % (len(frame), MAXFRAME))
        head = self._head()
        tail = self._tail()
        if (head + 1) & SLOT_MASK == tail:
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
        self._set_head(head + 1)
        return True

    def _consume(self, off, tail):
        """Release slot `tail`: clear its ready flag, then advance tail. Called
        for good and bad slots alike -- that is what keeps the ring unwedgeable."""
        self.wr(off + 0, struct.pack(">I", 0))
        self._set_tail(tail + 1)

    def get(self):
        """Consumer: dequeue one frame, or None when the ring is empty.

        Raises RingError for a slot that fails validation. The slot has ALREADY
        been consumed when that happens, so the next call moves on to the next
        slot; the caller only needs to count it.

        len == 0 with head != tail is a protocol violation, not a race: both
        producers write len (with a barrier) before they advance head, and both
        access paths -- posted PCIe writes from the host, `sync`-ordered stores
        on the OCTEON -- keep that order. It used to be treated as "not visible
        yet" and left in place, which is exactly the wedge.
        """
        head = self._head()
        tail = self._tail()
        if head == tail:
            return None
        off = self.b + slot_off(tail)
        ln = struct.unpack(">I", self.rd(off + 0, 4))[0]
        if ln == 0 or ln > MAXFRAME:
            self._consume(off, tail)
            raise RingError("slot %d len %d out of range (consumed)" % (tail, ln))
        crc = struct.unpack(">I", self.rd(off + 4, 4))[0]
        # Copy the payload out BEFORE releasing the slot: once tail moves the
        # producer may overwrite it. The CRC is then checked on our copy only.
        data = self.rd(off + SLOT_HDR, ln)
        self._consume(off, tail)
        if zlib.crc32(data) & 0xffffffff != crc:
            raise RingError("slot %d CRC mismatch (consumed)" % tail)
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


# ---- ingress filter --------------------------------------------------------
#
# The ring is a trust boundary, not just a wire: whatever comes out of it is
# handed to this host's kernel as if it arrived on a NIC. The link interface
# runs with route_localnet=1 (it has to, the addresses are in 127/8), and that
# sysctl also makes a packet to 127.0.0.1 arriving on the interface deliverable
# locally. So without a filter, the far end -- and anything the far end forwards
# for, i.e. the DP -- can reach every service this host binds to loopback.
#
# The daemon therefore admits only what the link exists to carry: IPv4 from a
# known peer address TO THIS HOST'S LINK ADDRESS, and ARP for that address from
# a known peer. Everything else -- other ethertypes, VLAN tags, IPv6, loopback
# or broadcast destinations, spoofed sources -- is dropped before the kernel
# sees it. This is enforced here in the daemon, independent of rp_filter or
# netfilter, because the daemon is the one place every frame passes through.

ETH_IPV4 = 0x0800
ETH_ARP = 0x0806


def ip4(s):
    """'a.b.c.d' -> int."""
    p = s.split(".")
    if len(p) != 4:
        raise ValueError("bad IPv4 address %r" % s)
    v = 0
    for q in p:
        n = int(q)
        if not 0 <= n <= 255:
            raise ValueError("bad IPv4 address %r" % s)
        v = (v << 8) | n
    return v


def cidr(s):
    """'a.b.c.d/n' or 'a.b.c.d' -> (network, mask) as ints."""
    if "/" in s:
        a, n = s.split("/", 1)
        n = int(n)
    else:
        a, n = s, 32
    if not 0 <= n <= 32:
        raise ValueError("bad prefix length in %r" % s)
    mask = (0xffffffff << (32 - n)) & 0xffffffff
    return ip4(a) & mask, mask


def ingress_ok(frame, local, allowed_src):
    """Decide whether a frame from the ring may be handed to the kernel.

    local       -- this host's address on the link, as an int (see ip4()).
    allowed_src -- iterable of (network, mask) int pairs (see cidr()).

    Pure function over immutable inputs: the frame is a bytes copy already
    taken out of the ring, so nothing here can change under our feet.
    """
    n = len(frame)
    if n < 14:
        return False
    et = (frame[12] << 8) | frame[13]

    if et == ETH_IPV4:
        if n < 34 or (frame[14] >> 4) != 4:
            return False
        ihl = (frame[14] & 0x0f) * 4
        if ihl < 20 or n < 14 + ihl:
            return False
        src = int.from_bytes(frame[26:30], "big")
        dst = int.from_bytes(frame[30:34], "big")
        if dst != local:
            return False
        return any((src & m) == net for net, m in allowed_src)

    if et == ETH_ARP:
        if n < 42:
            return False
        htype = (frame[14] << 8) | frame[15]
        ptype = (frame[16] << 8) | frame[17]
        if htype != 1 or ptype != ETH_IPV4 or frame[18] != 6 or frame[19] != 4:
            return False
        spa = int.from_bytes(frame[28:32], "big")
        tpa = int.from_bytes(frame[38:42], "big")
        if tpa != local:
            return False
        return any((spa & m) == net for net, m in allowed_src)

    return False
