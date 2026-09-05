#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 FreeFlow Networks, Inc.
"""ffn_config_render -- turn ffn-manager's committed config into the key/value
set that ffn_cfgd publishes to the CP and the DP.

This closes the last open link in a chain that was otherwise already complete:

    ffn-manager  (running-config.xml + the SQL domain tables)
      -> HERE                     renders /etc/ffn/config.env
        -> ffn_cfgd     (MP)      serves it, versioned, namespaced
          -> ffn_cfgagent (CP)    applies cp.*, relays dp.*
            -> PCIe mailbox       store-and-forward; no IP path to the DP
              -> DP /etc/ffn/dp.env
                -> ffn_dp_l3_config.c -> dp_l3_route_add() / neigh / iface

Everything below the render step already existed and works. The manager simply
never wrote config.env, so the relay delivered an empty set. This is that step,
and nothing else -- deliberately, because the transport underneath is proven and
changing it would risk a working path.

THREE RULES THAT SHAPE EVERY RENDERER HERE
------------------------------------------

1. **Emit the vocabulary the DP already parses.** `ffn_dp_l3_config.c` matches
   on exactly three prefixes -- `dp.l3.route.`, `dp.l3.neigh.`, `dp.l3.iface.`
   -- with an iproute2-shaped value. Inventing a tidier key would mean changing
   C on the dataplane, so the route renderer reproduces that grammar verbatim:

       dp.l3.route.<id>=<prefix>/<len> [via <gateway>] dev <egress>
       dp.l3.neigh.<id>=<ip> lladdr <mac>
       dp.l3.iface.<egress>.mac=<mac>

2. **Output must be deterministic.** ffn_cfgd bumps its version from the file's
   mtime, and every bump makes the CP re-pull and re-push to the DP over a
   64 KB mailbox. A renderer that emitted keys in dict order would churn the
   version on every commit that changed nothing, and store-and-forward to the DP
   is far too expensive to spend on noise. So: sorted keys, stable ids, and
   `write_if_changed` compares content rather than trusting the caller.

3. **Never render a secret.** config.env is relayed to two other processors and
   read by a busybox shell on the DP. Anything credential-shaped stays on the
   MP; the planes get the shape of the policy, not its keys. `SECRET_HINTS`
   below is the denylist, applied to every value regardless of which renderer
   produced it, so a new renderer cannot leak by omission.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET

DEF_DB = os.getenv("FFN_CONFIG_DB", "/var/lib/ffn-ngfw/config-v2.db")
DEF_XML = os.getenv("FFN_RUNNING_XML",
                    "/var/lib/ffn-ngfw/config/running-config.xml")
DEF_OUT = os.getenv("FFN_CONFIG_ENV", "/etc/ffn/config.env")

# Hand-authored keys the manager does not model, kept OUT of the generated file
# and merged underneath it.
#
# This layer exists because /etc/ffn/config.env was already a real, curated file
# -- BCM VLAN and STP port lists, FRR enablement, the DP session table size, the
# port map below -- none of which the manager has a UI for. A renderer that
# simply wrote its own output over that path would silently delete working
# configuration on the first commit. So the generated file is
# local-base + rendered, and the base is never written by this module.
DEF_LOCAL = os.getenv("FFN_CONFIG_LOCAL", "/etc/ffn/config.local.env")

# Maps an interface name to the DP's egress PORT INDEX.
#
# This is not cosmetic and it cannot be inferred. ffn_dp_l3_config.c parses the
# dev token with strtol(tok, &end, 10) and rejects the line unless *end == '\0':
#
#     } else if (strcmp(tok[i], "dev") == 0) {
#         dev = strtol(tok[i + 1], &end, 10);
#         if (*end != '\0' || dev < 0 || dev > 0xFFFF) { ... ERR_SYNTAX }
#
# So "dev enp11s0f0" is a SYNTAX ERROR on the dataplane, while looking perfectly
# well-formed everywhere upstream -- the exact failure that is invisible from
# the MP. The index is the DP's own port ordering (the order of -i IFACE args to
# ffn_dp_afpacket, which is also what the compiled policy indexes by), so only
# the operator can declare it. Declare it in the local base as:
#
#     dp.portmap.enp11s0f0=1
#     dp.portmap.enp11s0f1=3
#
# An interface with no mapping is SKIPPED and reported as a problem rather than
# emitted by name, because emitting the name would produce a config that is
# accepted by every layer except the one that matters.
PORTMAP_PREFIX = "dp.portmap."

# Manager-derived routes are keyed with this prefix so they cannot collide with
# hand-authored dp.l3.route.<n> entries in the local base. Safe because the DP
# never parses the id -- ffn_dp_l3_config.c matches only the "dp.l3.route."
# prefix and its header says the id exists solely to let distinct routes coexist
# as distinct keys.
MGR_ID = "mgr"

# Substrings that mark a value as never-relayable. Matched against the KEY, not
# the value: a value-based heuristic would either miss an innocuously-named
# secret or redact a legitimate config string that happens to look random.
SECRET_HINTS = ("secret", "password", "passwd", "key", "token", "psk",
                "private", "credential", "identity")

# A config.env value is one line, parsed by a busybox shell. Newlines would
# forge additional keys, so they are rejected rather than escaped -- an escaped
# newline in a shell-parsed file is a subtlety waiting to be got wrong.
_BAD_VALUE = re.compile(r"[\r\n]")


class RenderError(Exception):
    pass


def _clean(value):
    """Normalise a value, or return None if it must not be emitted."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if _BAD_VALUE.search(s):
        raise RenderError("value contains a newline: %r" % (s[:60],))
    return s


def _emit(out, key, value):
    """Add one key, dropping secrets and empties."""
    lk = key.lower()
    if any(h in lk for h in SECRET_HINTS):
        return
    v = _clean(value)
    if v is None:
        return
    out[key] = v


# ---------------------------------------------------------------------------
# Domain renderers. Each takes an open sqlite3 connection and/or the running
# XML root, and writes into `out`. They are separate so a domain can fail
# without taking the rest of the config with it -- see render().
# ---------------------------------------------------------------------------

def render_routes(out, db, root, portmap, problems, base):
    """static_routes -> dp.l3.route.<id>, in the DP's own grammar.

    The table columns line up with that grammar almost exactly -- dest_cidr,
    next_hop, dev -- which is why this is a direct translation and not a
    mapping layer. A row with no next_hop is a directly-connected route, which
    the DP encodes as nexthop 0; omitting "via" is how that is expressed, so an
    empty next_hop must produce no via clause rather than "via 0.0.0.0".
    """
    if db is None:
        return
    try:
        rows = db.execute(
            "SELECT id, dest_cidr, next_hop, dev, metric, table_id FROM "
            "static_routes ORDER BY id").fetchall()
    except sqlite3.Error:
        return
    aliases = _alias_map(root) if root is not None else {}
    for rid, cidr, nh, dev, _metric, table in rows:
        cidr = _clean(cidr)
        dev = _clean(dev)
        if not cidr or not dev:
            # Both are mandatory in the DP grammar. Skipping is right: a
            # half-rendered route would be parsed as something else.
            continue
        # PAN-OS name -> kernel name. The CP routes by NAME.
        dev = aliases.get(dev, dev)

        # The CP's own kernel route. Emitted independently of the portmap,
        # because only the DP needs an index -- withholding the CP route for a
        # missing DP mapping would stop the control plane routing over a
        # dataplane detail.
        nh = _clean(nh)
        if nh:
            cp_val = "%s via %s dev %s" % (cidr, nh, dev)
        else:
            cp_val = "%s dev %s" % (cidr, dev)
        # table 254 is the kernel's main table; naming it explicitly is
        # redundant and makes the route look VRF-scoped when it is not.
        if table and str(table) not in ("", "254"):
            cp_val += " table %s" % table
        _emit(out, "cp.route.%s%s" % (MGR_ID, rid), cp_val)

        # The DP addresses egresses by INDEX, so its copy needs the portmap.
        idx = portmap.get(dev)
        if idx is None:
            problems.append(
                "route %s: no %s%s -- the DP needs a numeric egress and would "
                "reject 'dev %s' as a syntax error, so the DP copy is not "
                "published (the CP route still is)" % (rid, PORTMAP_PREFIX, dev, dev))
            continue
        val = ("%s via %s dev %s" % (cidr, nh, idx) if nh
               else "%s dev %s" % (cidr, idx))
        _emit(out, "dp.l3.route.%s%s" % (MGR_ID, rid), val)


def _alias_map(root):
    """PAN-OS interface name -> Linux name, from <interface-alias>.

    This map is not cosmetic. The config speaks PAN-OS ("ethernet1/1") while a
    plane can only act on the kernel name ("enp11s0f0") -- `ip link set` and the
    DP's "dev" clause both want the latter. Rendering the PAN-OS label would
    produce keys that look right and match no interface anywhere.
    """
    m = {}
    for e in root.iterfind(".//interface-alias/entry"):
        panos = e.get("name")
        linux = (e.findtext("linux-name") or "").strip()
        if panos and linux:
            m[panos] = linux
    return m


def render_interfaces(out, db, root, portmap, problems, base):
    """Per-interface settings, keyed by the name the planes can act on.

    Interfaces come from the running XML rather than a table: that is where the
    manager keeps the canonical <interface> tree, and net_resources (management
    profiles) is folded into it at commit time, so by this point the XML is the
    complete picture.

    Shape actually present on this box:

        <entry name="ethernet1/1">
          <comment/>
          <layer3>
            <interface-management-profile>Development</interface-management-profile>
            <ip><entry name="10.0.0.1/24"/></ip>
          </layer3>
        </entry>

    Note the address is an <entry name="..."> ATTRIBUTE, not element text --
    ElementTree cannot select that with findtext(), which is exactly the bug the
    first version of this function had.

    Every key is emitted under the LINUX name, with the PAN-OS name carried
    alongside so an operator can trace a key back to what they typed in the UI.
    An interface with no alias is skipped rather than guessed at: acting on the
    wrong interface is worse than not acting.
    """
    if root is None:
        return
    aliases = _alias_map(root)
    _emit(out, "cp.iface.count", len(aliases))

    for ifnode in root.iterfind(".//network/interface/ethernet/entry"):
        panos = ifnode.get("name")
        if not panos:
            continue
        linux = aliases.get(panos)
        if not linux:
            # No alias means the manager has not bound this PAN-OS interface to
            # a real NIC. Emitting it would name something that does not exist.
            continue

        l3 = ifnode.find("./layer3")
        addrs = []
        if l3 is not None:
            for ipe in l3.iterfind("./ip/entry"):
                a = _clean(ipe.get("name"))
                if a:
                    addrs.append(a)
        mtu = ifnode.findtext("./layer3/mtu") or ifnode.findtext("./mtu")
        prof = ifnode.findtext("./layer3/interface-management-profile")
        comment = ifnode.findtext("./comment")
        # Checked in several places because PAN-OS has no canonical home for a
        # MAC on an ethernet interface -- it is hardware, not configuration --
        # so the manager may record it under layer3 or at the entry. The DP
        # genuinely needs it (it builds the L3 rewrite header from it), so
        # accept any of them rather than depend on one that may not be used.
        mac = (ifnode.findtext("./layer3/mac")
               or ifnode.findtext("./mac")
               or ifnode.findtext("./layer3/hw-address")
               or ifnode.findtext("./hw-address"))

        _emit(out, "cp.iface.%s.panos_name" % linux, panos)
        # Comma-joined rather than one key per address: the set of addresses on
        # an interface is applied as a unit, and indexed keys would make
        # removing the second of three look like an edit to the third.
        if addrs:
            _emit(out, "cp.iface.%s.addr" % linux, ",".join(addrs))
        _emit(out, "cp.iface.%s.mtu" % linux, mtu)
        _emit(out, "cp.iface.%s.mgmt_profile" % linux, prof)
        _emit(out, "cp.iface.%s.comment" % linux, comment)
        if mac:
            # dp.l3.iface.<egress>.mac -- and <egress> is the numeric port
            # index, not a name: ffn_dp_l3_config.c does
            # strtol(idp, &end, 10) and then requires strcmp(end, ".mac") == 0,
            # so "dp.l3.iface.enp11s0f0.mac" never matches anything.
            idx = portmap.get(linux)
            if idx is None:
                problems.append(
                    "iface %s: no %s%s, so its MAC is not published to the DP"
                    % (linux, PORTMAP_PREFIX, linux))
            else:
                _emit(out, "dp.l3.iface.%s.mac" % idx, mac)


def render_routers(out, db, root, portmap, problems, base):
    """virtual_routers -> which router owns which table, and its identity.

    The DP forwarder has ONE FIB and no notion of multiple virtual routers, so
    this does not attempt to render VRFs down to it. What it renders is the
    information the CP needs to keep FRR and the routing tables straight, plus
    a count the DP can use to notice it is being fed from a multi-VR setup it
    cannot represent -- better an explicit mismatch than silent merging.
    """
    try:
        rows = db.execute(
            "SELECT id, name, table_id, admin_up, router_id, asn FROM "
            "virtual_routers ORDER BY id").fetchall()
    except sqlite3.Error:
        return
    active = 0
    for rid, name, table_id, admin_up, router_id, asn in rows:
        name = _clean(name)
        if not name:
            continue
        if admin_up in (0, "0", False):
            continue
        active += 1
        _emit(out, "cp.vr.%s.name" % rid, name)
        _emit(out, "cp.vr.%s.table" % rid, table_id)
        _emit(out, "cp.vr.%s.router_id" % rid, router_id)
        _emit(out, "cp.vr.%s.asn" % rid, asn)
    if active:
        _emit(out, "cp.vr.count", active)


def render_policy(out, db, root, portmap, problems, base):
    """Firewall rules -> a SUMMARY and a version, never the rules themselves.

    The DP does not consume rules as key/value: ffn_dp_afpacket_main.c loads a
    compiled `policy.bin` (type 0x40 "FPPO", built by ffn_fastpath_compile.py)
    with -p. Rendering nine rules as ninety keys would therefore produce
    something nothing reads, and would push a rules table through a 64 KB
    mailbox for no purpose.

    So the key/value channel carries the CONTROL signal -- how many rules, what
    the default is, and a version that changes when the ruleset changes -- and
    the compiled blob travels separately. The version is a content hash rather
    than a counter so that reverting a change returns to the previous version
    instead of inventing a new one, which keeps a revert from looking like a
    fresh push.
    """
    try:
        rows = db.execute(
            "SELECT id, position, name, action, src_iface, dst_iface, proto, "
            "enabled FROM policy_rules WHERE enabled=1 "
            "AND COALESCE(hidden,0)=0 ORDER BY position, id").fetchall()
    except sqlite3.Error:
        return

    import hashlib
    h = hashlib.sha256()
    zt = 0
    for r in rows:
        h.update(("|".join("" if x is None else str(x) for x in r)).encode())
        # A ZeroTier rule is one matching the zt* interface pattern. Counted so
        # an operator can see from the plane's own view whether ZeroTier
        # traffic is admitted, without shipping the rule itself.
        if any(isinstance(x, str) and x.startswith("zt") for x in (r[4], r[5])):
            zt += 1

    _emit(out, "dp.policy.rules", len(rows))
    _emit(out, "dp.policy.version", h.hexdigest()[:16])
    if zt:
        _emit(out, "dp.policy.zerotier_rules", zt)
    # The DP's own default when no rule matches. It has a compiled-in default,
    # but rendering it makes the intended value explicit at the source rather
    # than implicit in whatever the binary was built with.
    _emit(out, "dp.default_decision",
          _running_text(root, "./devices/entry/deviceconfig/setting/default-decision")
          or "drop")


# What the DP's DLP scanner can actually match. Deliberately NOT a superset of
# the manager's pattern_type column: the dataplane runs this per packet on a
# forwarding path, so a pattern language with catastrophic backtracking is a
# denial of service against the forwarder wearing a nicer name. Structural and
# literal matching only -- see octeon/dpfwd/ffn_dp_engine.h.
DLP_TYPES = {"keyword", "credit_card", "ssn", "api_key"}
DLP_ACTIONS = {"alert", "block", "reset"}
DLP_DIRECTIONS = {"ingress", "egress", "any"}

# ffn_dp_dlp.h: DP_DLP_RULE_MAX and DP_DLP_PAT_MAX. Storage is fixed and inline
# because the dataplane does not allocate, so a policy that outgrows it must
# fail loudly HERE -- at config time, where someone is watching -- rather than
# quietly start dropping rules on the packet path.
DLP_RULE_MAX = 32
DLP_PAT_MAX = 63


def _faceplate_module():
    """The chassis faceplate map, or None when this is not that hardware.

    Imported lazily and never fatally: this renderer has to run on a box with no
    platform submodule, and a missing one means "software datapath", which is a
    fact about the host rather than an error.
    """
    try:
        import ffn_bcmports
        return ffn_bcmports
    except Exception:
        return None


def render_vsys(out, db, root, portmap, problems, base):
    """Virtual systems -> the keys the dataplane needs to enforce them.

        dp.vsys.tenant.<id> = <name>
        dp.vsys.port.<idx>  = <id>
        dp.vsys.count       = <n>

    WHY THESE TWO KEYS AND NOT MORE. The dataplane does not need to know what a
    virtual system IS. It needs to know which tenants exist -- so it can plan one
    SSO group, one QPG entry and one PKI style each -- and which port belongs to
    which. Everything else about a vsys (zones, rulebases, imports) is enforced
    through policy.bin, which travels separately as a compiled blob.

    A vsys with NO interfaces imported is still emitted. It is a tenant an
    operator has created and will assign ports to, and planning its resources
    now means the assignment does not later have to renumber everyone else's
    styles -- which, because PCAM can only ADD to a style, could otherwise make
    a previously valid layout unreachable.

    Ports are emitted by DP INDEX, not by name, for the same reason
    render_interfaces does it: the index is what the dataplane can act on. An
    interface with no portmap entry is REPORTED rather than skipped quietly --
    a port silently left in the wildcard vsys is a tenant boundary that is not
    being enforced, which is exactly the kind of thing that must not fail
    silently in a firewall.
    """
    if root is None:
        return

    aliases = _alias_map(root)
    tenants = []
    for e in root.findall("./devices/entry/vsys/entry"):
        name = (e.get("name") or "").strip()
        m = re.match(r"^vsys([0-9]+)$", name.lower())
        if not m:
            problems.append("vsys %r: name is not vsys<N>, so it has no stable "
                            "numeric id; not published" % name)
            continue
        vid = int(m.group(1))
        # 0 is dp_classify's wildcard -- a tenant with that id would match every
        # other tenant's traffic. 32 is DP_VSYS_MAX in ffn_dp_vsys.h.
        if vid < 1 or vid > 32:
            problems.append("vsys %r: id %d outside 1..32; not published"
                            % (name, vid))
            continue
        tenants.append((vid, name, e))

    if not tenants:
        return

    for vid, name, e in sorted(tenants):
        _emit(out, "dp.vsys.tenant.%d" % vid, name)
        for m in e.findall("./import/network/interface/member"):
            pan = (m.text or "").strip()
            if not pan:
                continue
            # The vsys import list names interfaces the way an operator does.
            # Resolve through the same alias map the rest of this file uses, so
            # a port means the same thing in every key.
            # TWO SHAPES, because the two platforms address a port differently.
            #
            # On a software datapath every port is a netdev the forwarder opens,
            # so a tenant is addressed by DP PORT INDEX -- dp.vsys.port.<idx>.
            #
            # On a chassis whose faceplate is on a switch ASIC the DP has no
            # per-faceplate interface at all: it sees a 40G trunk with about
            # four BGX ports behind it, and the faceplate port a frame came from
            # arrives in the SOURCE PORT field of the switch's TM header
            # ([dest16][src16] -- see bcm/ITMH.md). So the tenant has to be keyed
            # on the BCM LOGICAL PORT, which is what that field carries, and
            # dp.vsys.port.<idx> cannot express it: there is no DP index for
            # faceplate port 7 because the DP has no such port.
            #
            # Emitting the wrong one would look configured and enforce nothing.
            bp = _faceplate_module()
            if bp is not None:
                lport = bp.port_of_pan_ifname(pan)
                if lport is None:
                    problems.append(
                        "vsys %s imports %s, which is not a faceplate port on "
                        "this chassis -- no tenant boundary can be enforced for "
                        "it" % (name, pan))
                    continue
                _emit(out, "dp.vsys.fport.%s" % lport, vid)
                continue

            dev = aliases.get(pan, pan)
            idx = portmap.get(dev)
            if idx is None:
                problems.append(
                    "vsys %s imports %s, which has no dp.portmap entry -- that "
                    "port will stay in the wildcard vsys, so its tenant "
                    "boundary is NOT enforced" % (name, pan))
                continue
            _emit(out, "dp.vsys.port.%s" % idx, vid)

    _emit(out, "dp.vsys.count", len(tenants))


def render_engines(out, db, root, portmap, problems, base):
    """dlp_rules -> dp.dlp.rule.<id>, and the engine enable that arms them.

        dp.dlp.rule.<id> = <type>:<action>:<direction>:<pattern>
        dp.engine.dlp.enable = 1

    The engine registers DISABLED, so rules alone change nothing: arming it is
    a separate, explicit key. That means a config carrying rules but no enable
    is inert, which is the safe direction for the mistake to fall.

    A rule the dataplane cannot express is REPORTED, not dropped silently.
    That is the whole point of this function. The manager's pattern_type
    column defaults to 'regex', and the DP has no regex engine by design --
    rendering such a rule as something else, or omitting it quietly, would
    leave an operator looking at a rule in the WebUI marked enabled while
    nothing on the wire enforces it. Better to publish the rules that work and
    say plainly which ones did not.
    """
    try:
        rows = db.execute(
            "SELECT id, name, pattern_type, pattern, action, direction, enabled "
            "FROM dlp_rules WHERE enabled=1 ORDER BY id").fetchall()
    except sqlite3.Error:
        return

    rendered = 0
    scoped = 0
    for rid, name, ptype, pattern, action, direction, _en in rows:
        label = name or ("rule %s" % rid)
        ptype = (ptype or "").strip().lower()
        action = (action or "").strip().lower()
        direction = (direction or "any").strip().lower()
        pattern = (pattern or "").strip()

        if ptype not in DLP_TYPES:
            problems.append(
                "dlp %r: pattern_type %r is not enforceable in the dataplane "
                "(it matches literally and structurally, never by regex); "
                "rule NOT published" % (label, ptype))
            continue
        if action not in DLP_ACTIONS:
            problems.append("dlp %r: action %r is not one of %s; rule NOT published"
                            % (label, action, "/".join(sorted(DLP_ACTIONS))))
            continue
        if direction not in DLP_DIRECTIONS:
            problems.append("dlp %r: direction %r is not one of %s; rule NOT published"
                            % (label, direction, "/".join(sorted(DLP_DIRECTIONS))))
            continue
        if ptype == "keyword" and not pattern:
            problems.append("dlp %r: a keyword rule needs a keyword; rule NOT published"
                            % (label,))
            continue
        if len(pattern) > DLP_PAT_MAX:
            problems.append("dlp %r: pattern is %d bytes, the dataplane holds %d; "
                            "rule NOT published" % (label, len(pattern), DLP_PAT_MAX))
            continue
        if rendered >= DLP_RULE_MAX:
            problems.append("dlp: more than %d enabled rules; %r and any after it "
                            "were NOT published" % (DLP_RULE_MAX, label))
            break

        # The pattern runs to the end of the value, so a ':' inside it is fine
        # and needs no escaping -- the DP splits only the first three fields.
        _emit(out, "dp.dlp.rule.%s%s" % (MGR_ID, rid),
              "%s:%s:%s:%s" % (ptype, action, direction, pattern))
        rendered += 1
        if direction != "any":
            scoped += 1

    # Arm the engine only when something is actually armed. Publishing
    # enable=1 with no rules would put a scanner on the packet path that can
    # never match, which costs budget on every inspected packet for nothing.
    _emit(out, "dp.engine.dlp.enable", "1" if rendered else "0")
    _emit(out, "dp.dlp.rules", rendered)

    # Direction scoping is advisory today: the forwarder passes
    # DP_DIR_UNKNOWN because nothing it holds answers "is this leaving the
    # protected network" -- the port table classifies hardware, not trust. A
    # direction-scoped rule therefore fires in BOTH directions. Said here, at
    # the place an operator's direction column is turned into config, rather
    # than only in a C comment they will never read.
    if scoped:
        problems.append(
            "dlp: %d published rule(s) are direction-scoped, but the dataplane "
            "cannot yet tell ingress from egress, so they match BOTH directions"
            % scoped)


def render_platform(out, db, root, portmap, problems, base):
    """The handful of keys that are simply true of this box."""
    # NOT hardcoded. The curated base already declares all.platform=pa5220
    # for this chassis, and a renderer asserting "pa5200" would quietly
    # contradict it. The base owns this key; we only supply it if absent.
    host = _running_text(root, "./devices/entry/deviceconfig/system/hostname")
    _emit(out, "cp.hostname", host)
    # ZeroTier reaches the planes as a policy shape, not a daemon config: the
    # manager models it as "traffic arriving on zt*" (ZT_IFACE_PATTERN), and
    # the network id and identity secret stay on the MP. Rendering the pattern
    # lets a plane recognise the traffic class without holding any credential.
    _emit(out, "all.zerotier.iface_pattern", "zt*")


def render_forwarding(out, db, root, portmap, problems, base):
    """IP forwarding for the CONTROL PLANE.

    This moved off the MP: ffn_manager.py used to run `sysctl -w
    net.ipv4.tcp_l3mdev_accept=1` itself. Under the MP/CP/DP split the
    management plane holds config and orchestrates; it does not forward, and
    should carry none of the machinery for it.

    The intent is DERIVED rather than invented: a box with an admin-up virtual
    router is a router, and a router that does not forward is broken. So
    forwarding follows from the presence of a VR.

    The curated base wins if it says otherwise. That matters because forwarding
    is exactly the kind of thing an operator may need to hold down during a
    migration, and a renderer that overrode them would be fighting the person
    trying to keep the box up.
    """
    for k in ("cp.forwarding.ipv4", "cp.forwarding.ipv6",
              "cp.forwarding.l3mdev", "cp.forwarding.rp_filter"):
        if k in base:
            continue                      # operator has spoken; leave it
        if k == "cp.forwarding.ipv4":
            _emit(out, k, "1" if out.get("cp.vr.count") else "0")
        elif k == "cp.forwarding.l3mdev":
            # Only meaningful once a real VRF DEVICE exists. A virtual router on
            # table 254 (main) creates none -- 30-vrf explicitly skips it -- so
            # requesting l3mdev there just warns about a sysctl this kernel may
            # not even expose (it needs CONFIG_NET_L3_MASTER_DEV). Emit it only
            # when some VR uses a non-main table.
            if any(v not in ("", "254") for kk, v in out.items()
                   if kk.startswith("cp.vr.") and kk.endswith(".table")):
                _emit(out, k, "1")


def _running_text(root, path):
    if root is None:
        return None
    node = root.find(path)
    return node.text if node is not None else None


RENDERERS = (
    ("platform", render_platform),
    ("interfaces", render_interfaces),
    ("routes", render_routes),
    ("routers", render_routers),
    # after routers: it derives forwarding from cp.vr.count
    ("forwarding", render_forwarding),
    ("policy", render_policy),
    ("vsys", render_vsys),
    ("engines", render_engines),
)


# ---------------------------------------------------------------------------

def load_local(path=DEF_LOCAL):
    """Read the hand-authored base as an ordered key->value dict.

    Comments and blanks are dropped; the file is the operator's, so a malformed
    line is reported rather than guessed at.
    """
    base, problems = {}, []
    if not path or not os.path.exists(path):
        return base, problems
    try:
        with open(path) as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    problems.append("%s:%d: not key=value: %r" % (path, n, line[:50]))
                    continue
                k, v = line.split("=", 1)
                base[k.strip()] = v.strip()
    except OSError as exc:
        problems.append("local base: %s" % exc)
    return base, problems


def render(db_path=DEF_DB, xml_path=DEF_XML, local_path=DEF_LOCAL):
    """Render every domain over the hand-authored base. Returns (lines, problems).

    Layering: the local base goes down first, then the manager-derived keys on
    top. So anything the manager models is authoritative, and anything it does
    NOT model -- BCM VLAN/STP port lists, FRR enablement, the DP session table
    size, the port map -- survives untouched. Without this the first commit
    would silently delete a curated config the manager has no UI for.

    A failing domain does NOT abort the render. The alternative -- refusing to
    publish anything because one table is malformed -- would take the CP and DP
    out of convergence over an unrelated fault, and they hold no config of
    their own to fall back on. So each domain is isolated and its failure is
    reported alongside the config that did render.
    """
    base, problems = load_local(local_path)

    # The port map lives in the base because only the operator knows the DP's
    # launch order. Values must be numeric; a bad one is dropped loudly rather
    # than passed through to become a dataplane syntax error.
    portmap = {}
    for k, v in base.items():
        if k.startswith(PORTMAP_PREFIX):
            name = k[len(PORTMAP_PREFIX):]
            if v.isdigit():
                portmap[name] = v
            else:
                problems.append("%s%s=%r is not a port index" % (PORTMAP_PREFIX, name, v))

    root = None
    if xml_path and os.path.exists(xml_path):
        try:
            root = ET.parse(xml_path).getroot()
        except (ET.ParseError, OSError) as exc:
            problems.append("running xml: %s" % exc)

    db = None
    if db_path and os.path.exists(db_path):
        try:
            db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        except sqlite3.Error as exc:
            problems.append("config db: %s" % exc)
    else:
        problems.append("config db not found: %s" % db_path)

    out = {}
    for name, fn in RENDERERS:
        try:
            fn(out, db, root, portmap, problems, base)
        except Exception as exc:
            problems.append("%s: %r" % (name, exc))
    if db is not None:
        db.close()

    merged = dict(base)
    merged.update(out)          # manager-derived keys win over the base
    lines = ["%s=%s" % (k, merged[k]) for k in sorted(merged)]
    return lines, problems


def write_if_changed(path, lines):
    """Write atomically, and only when the content actually differs.

    Both halves matter. Atomic because ffn_cfgd may read the file at any moment
    and a half-written config.env would be served as truth. Only-on-change
    because cfgd versions on mtime, and a needless bump costs a full re-push to
    the DP through the mailbox.
    """
    body = "\n".join(["# FFN config -- rendered by ffn_config_render; do not edit"]
                     + lines) + "\n"
    try:
        with open(path) as fh:
            if fh.read() == body:
                return False
    except OSError:
        pass
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def publish(db_path=DEF_DB, xml_path=DEF_XML, out_path=DEF_OUT,
            local_path=DEF_LOCAL):
    """Render and publish. Returns a dict suitable for a commit response."""
    lines, problems = render(db_path, xml_path, local_path)
    changed = write_if_changed(out_path, lines)
    scopes = {}
    for l in lines:
        scopes[l.split(".", 1)[0]] = scopes.get(l.split(".", 1)[0], 0) + 1
    return {"published": changed, "keys": len(lines), "path": out_path,
            "scopes": scopes, "problems": problems}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEF_DB)
    ap.add_argument("--xml", default=DEF_XML)
    ap.add_argument("--out", default=DEF_OUT)
    ap.add_argument("--local", default=DEF_LOCAL,
                    help="hand-authored base merged UNDER the rendered keys")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be published, write nothing")
    args = ap.parse_args()

    lines, problems = render(args.db, args.xml, args.local)
    if args.dry_run:
        for l in lines:
            print(l)
        for p in problems:
            print("# PROBLEM: %s" % p, file=sys.stderr)
        return 0
    res = publish(args.db, args.xml, args.out, args.local)
    print(json.dumps(res, indent=1))
    return 1 if res["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
