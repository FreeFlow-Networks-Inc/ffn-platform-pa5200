#!/usr/bin/env python3
"""ffn_bcmports -- the PA-5220 BCM88375 port table, and what each port is for.

Why this file exists
--------------------
Nothing on this board tells you what a port is. `ps` in the vendor diag shell
shows logical names (`xe5`, `xl24`, `ce3`) that match neither the faceplate
labels nor PAN's own port names, and the vendor's `config.bcm` describes the
chip under a *different chip's* suffix. Getting that wrong wastes hours: an
earlier pass here concluded "KR interface means the CP trunk" and built its
forwarding config on `xe8`, which is not the CP at all.

So this table is assembled from three sources, and every row says which.

1. `config.bcm`: `ucode_port_<N>.BCM88650=<PANNAME>:core_<c>.<ch>` gives logical
   port -> PAN's own port name. The `.BCM88650` suffix really does apply to our
   BCM88375, because the same file says so:

       #Make Arad SOC properties work for QMX, by mapping the BCM88375 suffix
       soc_family.BCM88375=BCM88650

2. `config.bcm`: `tm_port_header_type_{in,out}_<N>` gives the port's ROLE. This
   is the single most important column and the one that explains the board.

3. Measurement on live silicon -- sending an exact number of frames out an
   interface and reading the BCM counter table. Inference from interface type
   (KR/XFI/XLAUI) is NOT reliable; three of the guesses it produced were wrong.

The board is a Traffic Manager, not a switch
--------------------------------------------
Only ONE port (3, CGE0) has `in=ETH`. Everything else is `RAW` or `TM`:

    front panel   in=RAW  out=RAW_DSA   raw on ingress, DSA header added on egress
    CP / DP links in=TM   out=TM|RAW    expect a Broadcom TM/DSA header
    port 3        in=ETH  out=DSA_RAW   the only L2 Ethernet port

So the BCM does not do L2 lookups for the datapath. Front-panel traffic is
tagged with a DSA header and steered to the dataplane Octeon; the DP reads the
header to learn the source port, and writes one back to choose the egress port.
That is why plain VLAN/STP/LIF configuration produces receive counters and no
transmit: a frame injected without a TM header on a `TM` ingress port is
discarded in the pipeline, correctly, however the VLAN is configured.

Consequence for anyone testing: to inject on a TM port you must prepend the TM
header. To test pure L2 you must first change the port's header type, which is
an init-time SOC property, not a runtime knob.
"""

# role constants
FRONT = "front"      # faceplate data port
CP = "cp"            # link to the control-plane OCTEON (CN73XX)
DP = "dp"            # link to the dataplane OCTEON (CN78xx)
CPU = "cpu"          # the chip's own CPU port
RECYCLE = "recycle"  # internal recycle path
FABRIC = "fabric"    # ILKN / external-fabric
UNKNOWN = "unknown"  # linked and real, but not yet identified

# logical port -> (PAN name, core, channel, in_hdr, out_hdr, role, note)
PORTS = {
    0:  ("CPU",    0, 0,  "RAW", "RAW",      CPU,     "chip CPU port"),
    1:  ("XE24",   0, 1,  "RAW", "RAW_DSA",  FRONT,   ""),
    2:  ("XLGE11", 0, 2,  "RAW", "RAW_DSA",  UNKNOWN, "40G, links up; second 40G, not yet identified"),
    3:  ("CGE0",   1, 3,  "ETH", "DSA_RAW",  FRONT,   "100G QSFP28; the ONLY in=ETH port"),
    4:  ("XE36",   0, 4,  "TM",  "TM",       CP,      "MEASURED: CP eth1 (20 frames sent -> 20 counted)"),
    5:  ("XE37",   0, 5,  "TM",  "TM",       CP,      "MEASURED: CP eth0 (50 frames sent -> 50 counted)"),
    6:  ("XE29",   0, 6,  "RAW", "RAW_DSA",  FRONT,   ""),
    7:  ("XE64",   0, 7,  "RAW", "RAW_DSA",  FRONT,   "MEASURED: cabled to port 16"),
    8:  ("XE38",   0, 8,  "TM",  "RAW",      UNKNOWN, "KR+autoneg, up, low background RX; NOT the CP"),
    9:  ("XE39",   0, 9,  "TM",  "RAW",      UNKNOWN, "KR+autoneg, up, low background RX; NOT the CP"),
    10: ("XE57",   0, 10, "RAW", "RAW_DSA",  FRONT,   ""),
    11: ("XE65",   0, 11, "RAW", "RAW_DSA",  FRONT,   ""),
    12: ("CGE1",   0, 12, "TM",  "RAW",      UNKNOWN, "100G, TM header"),
    13: ("XE33",   1, 13, "RAW", "RAW_DSA",  FRONT,   ""),
    14: ("XE34",   1, 14, "RAW", "RAW_DSA",  FRONT,   ""),
    15: ("XE35",   1, 15, "RAW", "RAW_DSA",  FRONT,   ""),
    16: ("XE25",   0, 16, "RAW", "RAW_DSA",  FRONT,   "MEASURED: cabled to port 7"),
    17: ("RCY",    0, 17, "TM",  "RAW",      RECYCLE, "recycle port -- why there is no 'xe17' in ps"),
    18: ("XE26",   0, 18, "RAW", "RAW_DSA",  FRONT,   ""),
    19: ("XE27",   0, 19, "RAW", "RAW_DSA",  FRONT,   ""),
    20: ("ILKN4",  0, 20, "TM",  "TM",       FABRIC,  "Interlaken; no ELK device fitted"),
    21: ("XE31",   0, 21, "RAW", "RAW_DSA",  FRONT,   ""),
    22: ("XE30",   0, 22, "RAW", "RAW_DSA",  FRONT,   ""),
    23: ("XE28",   0, 23, "RAW", "RAW_DSA",  FRONT,   ""),
    24: ("XLGE17", 0, 24, "TM",  "RAW",      DP,      "MEASURED: the DP OCTEON (its 40G eth0)"),
    25: ("XLGE13", 0, 25, "TM",  "RAW",      DP,      "second DP link, down"),
    26: ("XLGE10", 0, 26, "TM",  "RAW",      DP,      "third DP link, down"),
    27: ("XE67",   0, 27, "RAW", "RAW_DSA",  FRONT,   ""),
    28: ("XE32",   1, 28, "RAW", "RAW_DSA",  FRONT,   ""),
    29: ("XE56",   0, 29, "RAW", "RAW_DSA",  FRONT,   ""),
    30: ("XE59",   0, 30, "RAW", "RAW_DSA",  FRONT,   ""),
    31: ("XE58",   0, 31, "RAW", "RAW_DSA",  FRONT,   ""),
    32: ("CGE3",   1, 32, "RAW", "RAW_DSA",  FRONT,   ""),
    33: ("CGE5",   1, 33, "RAW", "RAW_DSA",  FRONT,   ""),
    34: ("CGE4",   1, 34, "RAW", "RAW_DSA",  FRONT,   "MEASURED: cabled to port 35"),
    35: ("CGE2",   1, 35, "RAW", "RAW_DSA",  FRONT,   "MEASURED: cabled to port 34"),
    36: ("XE66",   0, 36, "RAW", "RAW_DSA",  FRONT,   ""),
}

# Cables found by disabling one end and watching the other drop. Not faceplate
# labels -- the operator's "1 to 3, 5 to 13, 23 to 24" do not correspond to
# these logical numbers, which is exactly why this was measured rather than
# assumed.
LOOPBACKS = [(7, 16), (34, 35)]


def diag_name(port):
    """The name the vendor diag shell (`ps`) uses for a logical port.

    Not derivable from the PAN name: the shell names by speed class, so
    XE37 -> xe5 and XLGE17 -> xl24. Port 17 (RCY) has no diag name at all,
    which is the gap that makes `ps` numbering look sparse.
    """
    if port == 17 or port == 0:
        return None
    pan = PORTS[port][0]
    if pan.startswith("XLGE"):
        return "xl%d" % port
    if pan.startswith("CGE"):
        return "ce%d" % port
    if pan.startswith("ILKN"):
        return "il%d" % port
    return "xe%d" % port


def dense_index(port):
    """The index `vlan gport add ... PortID=` wants.

    That command counts real ports densely while `ps` numbering skips 17 (RCY),
    so the two agree below 17 and differ by one above it. Getting this wrong
    silently configures the neighbouring port: PortID=24 lands on xl25, not
    xl24.
    """
    return port if port < 17 else port - 1


def ports_by_role(role):
    return {p: v for p, v in PORTS.items() if v[5] == role}


def _fmt(port):
    pan, core, ch, ihdr, ohdr, role, note = PORTS[port]
    return "%-4s %-8s %-7s core_%d.%-3d %-4s %-9s %-8s %s" % (
        port, diag_name(port) or "-", pan, core, ch, ihdr, ohdr, role, note)


if __name__ == "__main__":
    import sys
    want = sys.argv[1] if len(sys.argv) > 1 else None
    print("%-4s %-8s %-7s %-10s %-4s %-9s %-8s %s" % (
        "port", "diag", "PAN", "core.ch", "in", "out", "role", "note"))
    print("-" * 100)
    for p in sorted(PORTS):
        if want and PORTS[p][5] != want:
            continue
        print(_fmt(p))
    if not want:
        print("\nloopback cables (measured): " +
              ", ".join("%d<->%d" % ab for ab in LOOPBACKS))
        print("front-panel data ports: %d" % len(ports_by_role(FRONT)))
