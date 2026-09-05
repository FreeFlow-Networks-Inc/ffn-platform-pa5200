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
MP = "mp"            # link to the x86 management processor
RECYCLE = "recycle"  # internal recycle path
FABRIC = "fabric"    # ILKN / external-fabric
UNKNOWN = "unknown"  # linked and real, but not yet identified

# logical port -> (PAN name, core, channel, in_hdr, out_hdr, role, note)
PORTS = {
    0:  ("CPU",    0, 0,  "RAW", "RAW",      CPU,     "chip CPU port"),
    1:  ("XE24",   0, 1,  "RAW", "RAW_DSA",  FRONT,   ""),
    2:  ("XLGE11", 0, 2,  "RAW", "RAW_DSA",  UNKNOWN, "40G, links up; second 40G, not yet identified"),
    3:  ("CGE0",   1, 3,  "ETH", "DSA_RAW",  UNKNOWN, "the ONLY in=ETH port, and NOT in the vendor front-panel list"),
    4:  ("XE36",   0, 4,  "TM",  "TM",       CP,      "MEASURED: CP eth1 (20 frames sent -> 20 counted)"),
    5:  ("XE37",   0, 5,  "TM",  "TM",       CP,      "MEASURED: CP eth0 (50 frames sent -> 50 counted)"),
    6:  ("XE29",   0, 6,  "RAW", "RAW_DSA",  FRONT,   ""),
    7:  ("XE64",   0, 7,  "RAW", "RAW_DSA",  FRONT,   "MEASURED: connected to port 16 (medium unknown)"),
    8:  ("XE38",   0, 8,  "TM",  "RAW",      MP,      "MEASURED: MP enp8s0f1, MAC 00:0a:0b:0c:10:02"),
    9:  ("XE39",   0, 9,  "TM",  "RAW",      MP,      "MEASURED: MP enp8s0f0, MAC 00:0a:0b:0c:10:01"),
    10: ("XE57",   0, 10, "RAW", "RAW_DSA",  FRONT,   ""),
    11: ("XE65",   0, 11, "RAW", "RAW_DSA",  FRONT,   ""),
    12: ("CGE1",   0, 12, "TM",  "RAW",      FRONT,   "front panel per enable_fp_ports.c, labelled HSCI"),
    13: ("XE33",   1, 13, "RAW", "RAW_DSA",  FRONT,   ""),
    14: ("XE34",   1, 14, "RAW", "RAW_DSA",  FRONT,   ""),
    15: ("XE35",   1, 15, "RAW", "RAW_DSA",  FRONT,   ""),
    16: ("XE25",   0, 16, "RAW", "RAW_DSA",  FRONT,   "MEASURED: connected to port 7 (medium unknown)"),
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
    34: ("CGE4",   1, 34, "RAW", "RAW_DSA",  FRONT,   "MEASURED: connected to port 35 (medium unknown)"),
    35: ("CGE2",   1, 35, "RAW", "RAW_DSA",  FRONT,   "MEASURED: connected to port 34 (medium unknown)"),
    36: ("XE66",   0, 36, "RAW", "RAW_DSA",  FRONT,   ""),
}

# The vendor's own front-panel list, from /usr/share/broadcom/enable_fp_ports.c,
# whose comment also explains the !ena state: "The config.bcm file contains SOC
# properties that disable all front panel ports on bcm.user startup." This list
# is ground truth and overrides any role guessed from header type alone.
VENDOR_FRONT_PANEL = [28, 13, 14, 15, 16, 1, 18, 19, 6, 21, 22, 23,
                      7, 11, 36, 27, 10, 29, 30, 31, 32, 33, 34, 35, 12]

# ---------------------------------------------------------------------------
# Faceplate map: which CONNECTOR on the front of the chassis each logical port
# serves. This is what an operator reads off the metal, and therefore what the
# WebUI and ffn-cli must name interfaces by.
#
# VENDOR_FRONT_PANEL above says WHICH ports are on the faceplate. It does not
# say WHERE, and the order it is written in is the vendor enable script's, not
# the faceplate's. Two vendor files carry the position information, both on the
# CP under /usr/share/broadcom (read in place, never packaged):
#
#   config.bcm            `ucode_port_<logical>=<PANNAME>:core_<c>.<ch>` grouped
#                         by connector type, and its own comments state the
#                         lines within each group are in FACEPLATE order --
#                         "connected to front panel SFP+ ports in MIXED UP order
#                         as shown (XE25, XE24, XE26, XE27, XE31, etc.)".
#   bcm88375_board.soc    per-quad SerDes comments that name faceplate numbers.
#
# Where they agree, that is what is below. Where they do not, see the two
# conflicts recorded underneath -- neither is resolved by guessing.
#
# port -> (faceplate label, connector type, nominal Gb/s)
FACEPLATE = {
    # RJ45, ports 1-4. config.bcm: "connected to front panel RJ45 ports in
    # SEQUENTIAL order as shown (XE32, XE33, XE34, XE35)". board.soc agrees
    # ("XE32-35 (quad 8, to front panel ports 1-4 RJ45)").
    28: ("1",  "RJ45", 10),   # XE32
    13: ("2",  "RJ45", 10),   # XE33
    14: ("3",  "RJ45", 10),   # XE34
    15: ("4",  "RJ45", 10),   # XE35

    # SFP+, ports 5-20, in config.bcm's declaration order. board.soc's LANE-MAP
    # comments agree quad for quad; its POLARITY comments do not -- see below.
    16: ("5",  "SFP+", 10),   # XE25
    1:  ("6",  "SFP+", 10),   # XE24
    18: ("7",  "SFP+", 10),   # XE26
    19: ("8",  "SFP+", 10),   # XE27
    21: ("9",  "SFP+", 10),   # XE31
    6:  ("10", "SFP+", 10),   # XE29
    23: ("11", "SFP+", 10),   # XE28
    22: ("12", "SFP+", 10),   # XE30
    7:  ("13", "SFP+", 10),   # XE64
    11: ("14", "SFP+", 10),   # XE65
    36: ("15", "SFP+", 10),   # XE66
    27: ("16", "SFP+", 10),   # XE67
    10: ("17", "SFP+", 10),   # XE57
    29: ("18", "SFP+", 10),   # XE56
    30: ("19", "SFP+", 10),   # XE59
    31: ("20", "SFP+", 10),   # XE58

    # QSFP28, ports 21-24, in config.bcm's declaration order. CONTESTED --
    # board.soc numbers these differently. See QSFP_CONFLICT below.
    32: ("21", "QSFP28", 100),   # CGE3
    33: ("22", "QSFP28", 100),   # CGE5
    34: ("23", "QSFP28", 100),   # CGE4
    35: ("24", "QSFP28", 100),   # CGE2

    # Not a numbered data connector. Both files call it HSCI (High Speed Chassis
    # Interconnect) and neither gives it a faceplate number, so it does not get
    # an ethernet1/N slot -- naming it one would invite an operator to configure
    # the chassis interconnect as a data port.
    12: ("hsci", "QSFP28", 100),  # CGE1
}

# Conflict 1 -- RESOLVED, recorded so it is not re-litigated.
# bcm88375_board.soc contradicts ITSELF on the last two SFP+ quads: its Rx/Tx
# POLARITY comments say "XE56-59 (quad 14, to front panel ports 13-16)" and
# "XE64-67 (quad 16, to front panel ports 17-20)", while its LANE-MAP comments
# in the same file say the exact reverse. config.bcm's declaration order agrees
# with the LANE-MAP pair, so the POLARITY pair is the erroneous one: two
# independent readings against one. The map above follows the majority.
#
# Conflict 2 -- NOT RESOLVED. Do not guess, measure.
# The four QSFP28 cages are assigned differently by the two files:
#     config.bcm order:    CGE3, CGE5, CGE4, CGE2  ->  21, 22, 23, 24
#     board.soc comments:  CGE5->21, CGE3->22, CGE2->23, CGE4->24
# The map above follows config.bcm, because config.bcm is the file the chip is
# actually configured from, its comment ties list order to faceplate order
# explicitly, and board.soc has already been shown (conflict 1) to carry at
# least one wrong comment pair. That is a tie-break, not a measurement.
# To settle it: put a QSFP28 loopback in ONE known cage and read back which
# logical port reports link. One module, one reading, done.
QSFP_CONFLICT = {32: "22", 33: "21", 34: "24", 35: "23"}   # board.soc's reading


def faceplate_label(port):
    """Faceplate label for a logical port, or None if it is not on the front.

    A label, not a number: port 12 is labelled "hsci" because that is what the
    chassis and both vendor files call it.
    """
    ent = FACEPLATE.get(port)
    return ent[0] if ent else None


def pan_ifname(port):
    """PAN-OS-style interface name for a faceplate port, or None.

    ethernet1/N where N is the FACEPLATE number, so ethernet1/1 is the leftmost
    RJ45 and an operator can match the name to the metal without a lookup
    table. The logical port number is deliberately not used: logical 28 is
    faceplate 1, and naming it ethernet1/28 would be a name only the chip
    understands.

    HSCI returns "hsci" rather than an ethernet1/N slot -- see FACEPLATE.
    """
    label = faceplate_label(port)
    if label is None:
        return None
    if not label.isdigit():
        return label
    return "ethernet1/%s" % label


def port_of_pan_ifname(name):
    """Reverse of pan_ifname(). None if the name names no faceplate port."""
    for port in FACEPLATE:
        if pan_ifname(port) == name:
            return port
    return None


def faceplate_ports():
    """Logical ports that terminate on the front of the chassis, in faceplate
    order (numbered connectors first, then the named ones)."""
    def key(p):
        label = FACEPLATE[p][0]
        return (0, int(label)) if label.isdigit() else (1, label)
    return sorted(FACEPLATE, key=key)


# Consistency: every faceplate port must be a port the vendor's own enable list
# calls a front-panel port, and vice versa. A drift between the two means one of
# them has been edited without the other, and the WebUI would then either offer
# a port that cannot be enabled or hide one that can.
assert set(FACEPLATE) == set(VENDOR_FRONT_PANEL), (
    "FACEPLATE and VENDOR_FRONT_PANEL disagree: %r"
    % (set(FACEPLATE) ^ set(VENDOR_FRONT_PANEL),))
assert len(set(f[0] for f in FACEPLATE.values())) == len(FACEPLATE),     "duplicate faceplate label"


# Connected pairs, found by disabling one end and watching the other drop.
#
# "Connected", not "cabled": these ports were enabled for the first time in the
# same run that found them, so an internal trace and an operator's loopback
# cable are indistinguishable from here. The operator's stated "1 to 3, 5 to 13,
# 23 to 24" does not correspond to these logical numbers either way, which is
# why the pairing was measured rather than assumed.
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
        print("\nconnected pairs (measured): " +
              ", ".join("%d<->%d" % ab for ab in LOOPBACKS))
        print("front-panel data ports: %d" % len(ports_by_role(FRONT)))
