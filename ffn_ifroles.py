#!/usr/bin/env python3
"""ffn_ifroles -- which interfaces are control plane, and which are data plane.

The rule this encodes, for the PA-5200 family:

    NO host-visible NIC is a data-plane interface.

Every one of them is management-class or internal, which is why
/etc/ffn-ngfw/dataplane-README exists and why ffn-dp-afpacket is disabled here:
bridging AUX-1 to AUX-2 would join two management interfaces. The real data
ports live behind the Octeon/FE100 and reach the WebUI through the dataplane
port table (ffn_ifctl), not through the host's netdevs.

So control-plane adapters must not appear in the *network* interface list. They
belong under Devices > Setup, where an operator configures management, HA and
AUX addressing.

Keyed by **PCI address**, not by name: interface names shift between kernels and
reboots, PCI slots do not. Confirmed on the live PA-5220 -- the map below is
exactly what `/sys/class/net/*/device` reports there, and matches PAN's own
gryphon.py udev block, which defines these same seven and no more.

An operator can override any of it in /etc/ffn-ngfw/if-roles.json:

    {"control_plane_pci": {"0000:0f:00.0": ["MGT", "management"]},
     "data_plane_netdevs": ["enp3s0f0"]}

`data_plane_netdevs` is the escape hatch for a platform where a host NIC really
is a data port; it is empty on a 5200 and should stay that way.
"""
import json
import os

CONF = "/etc/ffn-ngfw/if-roles.json"

# role -> (label, class). class is what the UI groups by.
CONTROL_PLANE_PCI = {
    "0000:0f:00.0": ("MGT", "management"),
    "0000:10:00.0": ("HA1-A", "ha"),
    "0000:11:00.0": ("HA1-B", "ha"),
    "0000:0b:00.0": ("AUX-1", "aux"),
    "0000:0b:00.1": ("AUX-2", "aux"),
    "0000:08:00.0": ("BP-0", "internal"),
    "0000:08:00.1": ("BP-1", "internal"),
}

# Virtual/administrative interfaces are never data-plane either.
VIRTUAL_PREFIXES = ("lo", "zt", "docker", "veth", "br-", "virbr", "tun", "tap",
                    "wg", "tmfifo_net", "rshim", "dummy", "bond", "ovs-")


def _conf():
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def pci_of(netdev):
    """PCI address behind a netdev, or None for virtual interfaces."""
    try:
        p = os.path.realpath("/sys/class/net/%s/device" % netdev)
    except OSError:
        return None
    base = os.path.basename(p)
    # a real PCI device basename looks like 0000:0b:00.0
    return base if base.count(":") == 2 else None


def devtype(netdev):
    """DEVTYPE from the netdev's uevent: "vxlan", "vlan", "bridge", ...

    Read from uevent rather than guessed from the name, because an operator can
    call a VXLAN interface anything they like -- "overlay0", "tenant7" -- and a
    name-prefix test would classify it wrongly in exactly the case that matters.
    """
    try:
        with open("/sys/class/net/%s/uevent" % netdev) as f:
            for line in f:
                if line.startswith("DEVTYPE="):
                    return line.strip().split("=", 1)[1] or None
    except OSError:
        pass
    return None


def underlay_of(netdev):
    """The device a stacked netdev sits on, via its lower_<name> link."""
    try:
        for e in os.listdir("/sys/class/net/%s" % netdev):
            if e.startswith("lower_"):
                return e[len("lower_"):]
    except OSError:
        pass
    return None


def classify(netdev):
    """-> {role, label, class, control_plane, pci}

    Anything that is not explicitly declared a data-plane netdev is treated as
    control plane. Failing that way round is deliberate: mistaking a management
    port for a data port is how you bridge your own management network.
    """
    c = _conf()
    cp_map = dict(CONTROL_PLANE_PCI)
    for k, v in (c.get("control_plane_pci") or {}).items():
        cp_map[k] = tuple(v)
    data_plane = set(c.get("data_plane_netdevs") or [])

    pci = pci_of(netdev)
    if netdev in data_plane:
        return {"role": "data", "label": netdev, "class": "dataplane",
                "control_plane": False, "pci": pci}

    # ---- VXLAN is dataplane processing, and only dataplane ----------------
    # Operator requirement: VXLAN encap/decap belongs to the dataplane and
    # nowhere else. So a VXLAN netdev is classified data-plane -- but ONLY if
    # its underlay is a data-plane interface. Built over a management/HA/AUX
    # port the overlay would be riding the control-plane network, which is the
    # same mistake as putting a control-plane port in a security zone. That is
    # reported as a violation AND failed towards control plane, so the WebUI
    # will not offer it for forwarding while it is mis-parented.
    kind = devtype(netdev)
    if kind == "vxlan":
        under = underlay_of(netdev)
        if under and is_control_plane(under):
            return {"role": "vxlan", "label": netdev, "class": "unclassified",
                    "control_plane": True, "pci": pci, "devtype": kind,
                    "underlay": under,
                    "violation": "vxlan-on-control-plane-underlay"}
        return {"role": "vxlan", "label": netdev, "class": "dataplane",
                "control_plane": False, "pci": pci, "devtype": kind,
                "underlay": under}
    if pci and pci in cp_map:
        label, klass = cp_map[pci]
        return {"role": klass, "label": label, "class": klass,
                "control_plane": True, "pci": pci}
    if netdev.startswith(VIRTUAL_PREFIXES) or pci is None:
        return {"role": "virtual", "label": netdev, "class": "virtual",
                "control_plane": True, "pci": pci}
    # A host NIC we do not recognise. On this platform there is no such thing
    # as a host-side data port, so treat it as control plane and say so.
    return {"role": "unknown", "label": netdev, "class": "unclassified",
            "control_plane": True, "pci": pci}


def is_control_plane(netdev):
    return classify(netdev)["control_plane"]


def annotate(ifaces):
    """Add role fields to interface dicts that carry a "name"."""
    out = []
    for i in ifaces:
        d = dict(i)
        if d.get("type") == "octeon":
            # already a dataplane port from the DP table
            d.setdefault("control_plane", False)
            d.setdefault("label", d.get("name"))
            out.append(d)
            continue
        info = classify(d.get("name", ""))
        d.update({"role_label": info["label"], "role_class": info["class"],
                  "control_plane": info["control_plane"], "pci": info["pci"]})
        out.append(d)
    return out


def vxlan_violations(netdevs=None):
    """VXLAN interfaces built on a control-plane underlay.

    -> [(vxlan, underlay)]. Empty list means the requirement holds. Kept
    separate from classify() so a caller can refuse a whole configuration
    rather than silently reclassifying one interface.
    """
    if netdevs is None:
        try:
            netdevs = sorted(os.listdir("/sys/class/net"))
        except OSError:
            return []
    out = []
    for n in netdevs:
        c = classify(n)
        if c.get("violation") == "vxlan-on-control-plane-underlay":
            out.append((n, c.get("underlay")))
    return out


def split(ifaces):
    """-> (data_plane, control_plane)"""
    ann = annotate(ifaces)
    return ([i for i in ann if not i.get("control_plane")],
            [i for i in ann if i.get("control_plane")])


if __name__ == "__main__":
    import sys
    names = sys.argv[1:] or sorted(os.listdir("/sys/class/net"))
    print("%-12s %-14s %-8s %-12s %s"
          % ("netdev", "pci", "label", "class", "control plane"))
    for n in names:
        c = classify(n)
        print("%-12s %-14s %-8s %-12s %s"
              % (n, c["pci"] or "-", c["label"], c["class"],
                 "YES" if c["control_plane"] else "no (data plane)"))
    print()
    v = vxlan_violations(names)
    if v:
        print("VIOLATION -- VXLAN on a control-plane underlay:")
        for vx, un in v:
            print("  %s is built on %s (control plane)" % (vx, un))
        print("VXLAN must be dataplane-only. Re-parent it onto a data port.")
        print()
    print("On a PA-5200 every host NIC is control plane; the data ports live")
    print("behind the Octeon/FE100 and arrive via the dataplane port table.")
