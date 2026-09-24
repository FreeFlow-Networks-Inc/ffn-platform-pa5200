#!/usr/bin/env python3
"""ffn_pcnetd -- host (MP) end of the FFN PCIe virtual Ethernet.

Bridges a TAP interface (ffnnet0, 127.1.1.1/24) to the two rings in OCTEON DRAM,
reached through the index-1 BAR window. This is the exact peer of the OCTEON-side
ffn_pcnetd, speaking the protocol validated in test_pcnet.py.

The host owns region init: it resets the header and both rings at startup, then
waits for the OCTEON to set oct_up before reporting the link up. That way a host
restart cleanly re-establishes the link regardless of what the OCTEON left behind.

Userspace + per-frame MMIO is not fast, but it is correct and it is the whole
transport in ~150 lines. The bulk NFS direction (MP -> OCTEON) is host BAR
*writes*, which is the faster side of the aperture; the slow side (host reads for
OCTEON -> MP) carries only small NFS requests. Kernelise later if throughput
needs it.
"""
import argparse
import fcntl
import mmap
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_octdram as od
import ffn_pcnet_ring as pn

PCI = "0000:01:00.0"
BAR2_SLICE = 0x400000                     # index-1 aperture base within BAR2
TUNSETIFF = 0x400454ca
IFF_TAP = 0x0002
IFF_NO_PI = 0x1000


def point_window():
    """Point BAR1 index 1 at the region. Done once; it then stays put."""
    idxval = ((pn.BASE >> 22) << 4) | 3
    be = od.VendorCsrBackend()
    if not be.available:
        raise RuntimeError("oct-remote-csr unavailable; cannot point the window")
    got = be.write("spem0_bar1_index1", idxval)
    if got != idxval:
        raise RuntimeError("index1 did not take 0x%x (got %s)" % (idxval, got))


def open_region():
    fd = os.open("/sys/bus/pci/devices/%s/resource2" % PCI, os.O_RDWR | os.O_SYNC)
    m = mmap.mmap(fd, 0x800000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def rd(off, n):
        return bytes(m[BAR2_SLICE + off:BAR2_SLICE + off + n])

    def wr(off, data):
        m[BAR2_SLICE + off:BAR2_SLICE + off + len(data)] = data

    return rd, wr, m, fd


TUNSETPERSIST = 0x400454cb


def open_tap(name, persist=False):
    fd = os.open("/dev/net/tun", os.O_RDWR)
    ifr = struct.pack("16sH", name.encode(), IFF_TAP | IFF_NO_PI)
    fcntl.ioctl(fd, TUNSETIFF, ifr)
    if persist:
        # Keep the interface alive across daemon restarts, so routing can be
        # configured and debugged independently of the bridge loop.
        fcntl.ioctl(fd, TUNSETPERSIST, 1)
    os.set_blocking(fd, False)
    return fd


def ifup(name, cidr):
    for cmd in (["ip", "addr", "flush", "dev", name],
                ["ip", "addr", "add", cidr, "dev", name],
                ["ip", "link", "set", name, "up"],
                ["ip", "link", "set", name, "mtu", str(pn.MTU)]):
        subprocess.run(cmd, capture_output=True)
    # 127/8 is treated as loopback and would route to lo, never reaching this
    # interface -- that is exactly why the vendor uses 127.1.x (non-routable =
    # PCIe-only isolation), and route_localnet is what makes it usable on a real
    # interface. A host-scope route pins the peer to this device ahead of the
    # local table's 127.0.0.0/8 -> lo entry.
    #
    # ONLY on this interface. This used to also set conf/all/route_localnet,
    # which lifts the martian check for 127/8 on EVERY interface, physical NICs
    # included -- the exact isolation the NFS export and the CP shell rely on.
    # rp_filter is set strict on the link as well, so a source the kernel would
    # not route back out this interface is dropped (the effective value is the
    # max of conf/all and conf/<iface>, so this cannot be undone by a loose
    # global setting, only strengthened).
    for key, val in (("route_localnet", "1"), ("rp_filter", "1")):
        try:
            with open("/proc/sys/net/ipv4/conf/%s/%s" % (name, key), "w") as f:
                f.write(val)
        except OSError:
            pass
    # Disable segmentation offloads so the TAP never hands us a super-frame
    # larger than a ring slot.
    subprocess.run(["ethtool", "-K", name, "tso", "off", "gso", "off",
                    "gro", "off"], capture_output=True)
    peer = cidr.split("/")[0].rsplit(".", 1)[0] + ".2"   # 127.1.1.1 -> 127.1.1.2
    subprocess.run(["ip", "route", "replace", peer + "/32", "dev", name],
                   capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="ffnnet0")
    ap.add_argument("--addr", default="127.1.1.1/24")
    ap.add_argument("--wait-peer", type=float, default=0,
                    help="seconds to wait for oct_up before bridging (0 = don't block)")
    ap.add_argument("--skip-window", action="store_true",
                    help="assume BAR1 index 1 is already programmed (avoids oct-remote-csr)")
    ap.add_argument("--setup-only", action="store_true",
                    help="program window, region, interface, then exit (persistent TAP)")
    ap.add_argument("--allow-src", action="append", metavar="CIDR",
                    help="source addresses admitted from the ring (repeatable); "
                         "default: the CP (127.1.1.2) and the DP subnet "
                         "(127.1.2.0/24) it forwards for")
    ap.add_argument("--no-filter", action="store_true",
                    help="DEBUG ONLY: hand every ring frame to the kernel unfiltered")
    a = ap.parse_args()

    local_ip = pn.ip4(a.addr.split("/")[0])
    allowed = [pn.cidr(s) for s in (a.allow_src or ["127.1.1.2/32", "127.1.2.0/24"])]

    if not a.skip_window:
        point_window()
        print("ffn_pcnetd: BAR1 index 1 pointed at region", flush=True)
    rd, wr, m, memfd = open_region()

    # host owns region init
    pn.host_init(rd, wr)
    print("ffn_pcnetd: region reset at 0x%x, magic published" % pn.BASE, flush=True)

    tapfd = open_tap(a.iface, persist=a.setup_only)
    ifup(a.iface, a.addr)
    print("ffn_pcnetd: %s up at %s, mtu %d" % (a.iface, a.addr, pn.MTU), flush=True)

    if a.setup_only:
        print("ffn_pcnetd: setup-only, exiting (TAP persists)", flush=True)
        return 0

    if a.wait_peer:
        t0 = time.time()
        while time.time() - t0 < a.wait_peer:
            _, _, _, _, _, _, _, oup = pn.read_hdr(rd)
            if oup:
                print("ffn_pcnetd: OCTEON attached (oct_up=1)")
                break
            time.sleep(0.2)
        else:
            print("ffn_pcnetd: OCTEON not attached yet; bridging anyway")

    h2o = pn.Ring(pn.H2O_OFF, rd, wr)     # host produces
    o2h = pn.Ring(pn.O2H_OFF, rd, wr)     # host consumes

    print("ffn_pcnetd: bridging %s <-> rings; ingress %s" % (
        a.iface, "UNFILTERED" if a.no_filter else
        "to %s from %s" % (a.addr.split("/")[0],
                           ", ".join(a.allow_src or ["127.1.1.2", "127.1.2.0/24"]))),
        flush=True)
    # Poll both directions. The TAP is fd-pollable; the O2H ring is not, so use a
    # short poll timeout and check it every iteration.
    import select
    poller = select.poll()
    poller.register(tapfd, select.POLLIN)
    tx = rx = txdrop = rxdrop = rxfilt = 0
    last_stat = time.time()
    while True:
        # host -> OCTEON: drain the TAP into H2O
        events = poller.poll(1)          # 1 ms
        if events:
            for _ in range(64):          # bounded per iteration, then service RX
                try:
                    frame = os.read(tapfd, pn.SLOT)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if not frame:
                    break
                if len(frame) > pn.SLOT - 8:
                    # too big for a slot (e.g. a GSO super-frame slipped through);
                    # drop rather than raise. Offloads are disabled at ifup, so
                    # this should not happen, but a crash here is not acceptable.
                    txdrop += 1
                    continue
                try:
                    if h2o.put(frame):
                        tx += 1
                    else:
                        txdrop += 1
                except ValueError:
                    txdrop += 1

        # OCTEON -> host: drain O2H into the TAP
        for _ in range(64):
            try:
                frame = o2h.get()
            except pn.RingError:
                # Bad length or CRC. get() has already consumed the slot, so
                # this really does move on; it is counted, never retried.
                rxdrop += 1
                continue
            if frame is None:
                break
            # The frame is a private copy. Admit it only if it is addressed to
            # this host's link address from a known peer -- see ingress_ok().
            if not a.no_filter and not pn.ingress_ok(frame, local_ip, allowed):
                rxfilt += 1
                continue
            try:
                os.write(tapfd, frame)
                rx += 1
            except OSError:
                rxdrop += 1

        if time.time() - last_stat > 10:
            print("ffn_pcnetd: tx=%d rx=%d txdrop=%d rxdrop=%d rxfilt=%d"
                  % (tx, rx, txdrop, rxdrop, rxfilt), flush=True)
            last_stat = time.time()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
