#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit-test ffn_config_render against a synthetic config.

A synthetic fixture rather than the live box, for one reason: the appliance
currently has ZERO static routes, so the route renderer -- the one whose output
is parsed by C on the dataplane -- would otherwise never be exercised. The XML
shapes below are copied from the real running-config.xml (the <ip><entry
name="..."/></ip> form, the interface-alias tree), so this tests the shapes that
actually occur, not invented ones.

Run: python3 test_config_render.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_config_render as R  # noqa: E402

XML = """<?xml version="1.0"?>
<config>
  <devices><entry name="localhost.localdomain">
    <deviceconfig>
      <system>
        <hostname>ffn-lab</hostname>
        <interface-alias>
          <entry name="ethernet1/1"><linux-name>enp11s0f0</linux-name></entry>
          <entry name="ethernet1/2"><linux-name>enp11s0f1</linux-name></entry>
          <entry name="ethernet1/8"><linux-name>ffnnet0</linux-name></entry>
        </interface-alias>
      </system>
    </deviceconfig>
    <network><interface><ethernet>
      <entry name="ethernet1/1">
        <comment>uplink</comment>
        <layer3>
          <interface-management-profile>Development</interface-management-profile>
          <mtu>9000</mtu>
          <ip><entry name="10.0.0.1/24"/><entry name="10.0.1.1/24"/></ip>
          <mac>02:11:22:33:44:55</mac>
        </layer3>
      </entry>
      <entry name="ethernet1/2">
        <layer3><ip><entry name="192.168.5.1/24"/></ip></layer3>
      </entry>
      <entry name="ethernet1/9">
        <layer3><ip><entry name="172.16.0.1/24"/></ip></layer3>
      </entry>
    </ethernet></interface></network>
  </entry></devices>
</config>
"""


def build_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE static_routes (id INTEGER, vr_id INTEGER, "
               "dest_cidr TEXT, next_hop TEXT, dev TEXT, metric INTEGER, "
               "table_id INTEGER, created_at TEXT)")
    db.executemany("INSERT INTO static_routes VALUES (?,?,?,?,?,?,?,?)", [
        (1, 1, "0.0.0.0/0", "10.0.0.254", "ethernet1/1", 100, 254, ""),
        (2, 1, "192.168.5.0/24", None, "ethernet1/2", 0, 254, ""),   # connected
        (3, 1, "10.9.0.0/16", "10.0.0.9", "enp11s0f0", 0, 254, ""),  # kernel name
        (4, 1, None, None, "ethernet1/1", 0, 254, ""),               # invalid
        (5, 1, "10.8.0.0/16", "10.0.0.8", None, 0, 254, ""),         # invalid
    ])
    db.execute("CREATE TABLE virtual_routers (id INTEGER, name TEXT, "
               "table_id INTEGER, interfaces TEXT, admin_up INTEGER, vsys TEXT, "
               "protocol TEXT, router_id TEXT, asn INTEGER, frr_fragment TEXT, "
               "created_at TEXT, updated_at TEXT, vr_config TEXT)")
    db.executemany("INSERT INTO virtual_routers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, "default", 254, "", 1, "vsys1", "bgp", "10.0.0.1", 65001, "", "", "", ""),
        (2, "down-vr", 100, "", 0, "vsys1", "", "", None, "", "", "", ""),
    ])
    db.execute("CREATE TABLE policy_rules (id INTEGER, position INTEGER, "
               "name TEXT, src_ip TEXT, dst_ip TEXT, src_port TEXT, "
               "dst_port TEXT, proto TEXT, action TEXT, description TEXT, "
               "hit_count INTEGER, enabled INTEGER, kind TEXT, "
               "immutable INTEGER, hidden INTEGER, created_at TEXT, "
               "updated_at TEXT, src_iface TEXT, dst_iface TEXT)")
    db.executemany(
        "INSERT INTO policy_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (1, 0, "allow-zerotier", None, None, None, None, "any", "permit",
             "", 0, 1, "user", 0, 0, "", "", "zt*", None),
            (2, 1, "allow-lan", None, None, None, None, "any", "permit",
             "", 0, 1, "user", 0, 0, "", "", "enp11s0f0", None),
            (3, 2, "disabled", None, None, None, None, "any", "deny",
             "", 0, 0, "user", 0, 0, "", "", None, None),   # enabled=0
            (4, 3, "hidden", None, None, None, None, "any", "deny",
             "", 0, 1, "system", 0, 1, "", "", None, None),  # hidden=1
        ])
    db.commit()
    db.close()


def main():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "cfg.db")
    xmlp = os.path.join(tmp, "running.xml")
    build_db(dbp)
    with open(xmlp, "w") as fh:
        fh.write(XML)

    # The hand-authored base: keys the manager does not model, plus the port
    # map, which only the operator can declare (it is the DP's launch order).
    localp = os.path.join(tmp, "config.local.env")
    with open(localp, "w") as fh:
        fh.write("\n".join([
            "# curated base",
            "all.platform=pa5220",
            "cp.bcm.vlan1.ports=xe5,xe8",
            "dp.session.table.size=1048576",
            # a hand-authored route, in the DP's numeric-dev grammar
            "dp.l3.route.7=10.7.0.0/16 dev 3",
            "dp.portmap.enp11s0f0=1",
            "dp.portmap.enp11s0f1=3",
            # deliberately malformed: must be reported, not passed through
            "dp.portmap.bad=notanumber",
        ]) + "\n")

    lines, problems = R.render(dbp, xmlp, localp)
    # ethernet1/2 maps to enp11s0f1 which IS in the portmap, so the only
    # expected problem is the malformed portmap entry.
    check(len(problems) == 1 and "notanumber" in problems[0],
          "unexpected problems: %r" % (problems,))
    kv = dict(l.split("=", 1) for l in lines)

    # -- routes: the DP's grammar, exactly ---------------------------------
    check(kv.get("dp.l3.route.mgr1") == "0.0.0.0/0 via 10.0.0.254 dev 1",
          "route 1 (via + alias + portmap): %r" % kv.get("dp.l3.route.mgr1"))
    # No next_hop -> directly connected -> NO "via" clause. The DP encodes that
    # as nexthop 0; emitting "via 0.0.0.0" would be a different route.
    check(kv.get("dp.l3.route.mgr2") == "192.168.5.0/24 dev 3",
          "route 2 (connected): %r" % kv.get("dp.l3.route.mgr2"))
    # A dev already given as a kernel name must pass through untouched.
    check(kv.get("dp.l3.route.mgr3") == "10.9.0.0/16 via 10.0.0.9 dev 1",
          "route 3 (kernel name): %r" % kv.get("dp.l3.route.mgr3"))
    # every published dev must be a bare integer -- the DP rejects anything else
    for k, v in kv.items():
        if k.startswith("dp.l3.route."):
            dev = v.split("dev ", 1)[1].strip()
            check(dev.isdigit(), "route %s has non-numeric dev %r" % (k, dev))
    # Rows missing a mandatory field are dropped, not half-rendered.
    check("dp.l3.route.mgr4" not in kv, "route 4 (no cidr) should be dropped")
    check("dp.l3.route.mgr5" not in kv, "route 5 (no dev) should be dropped")
    # the hand-authored route must survive the merge untouched
    check(kv.get("dp.l3.route.7") == "10.7.0.0/16 dev 3",
          "hand-authored route was lost: %r" % kv.get("dp.l3.route.7"))

    # -- interfaces --------------------------------------------------------
    check(kv.get("cp.iface.enp11s0f0.panos_name") == "ethernet1/1",
          "alias not resolved: %r" % kv.get("cp.iface.enp11s0f0.panos_name"))
    check(kv.get("cp.iface.enp11s0f0.addr") == "10.0.0.1/24,10.0.1.1/24",
          "multi-address join: %r" % kv.get("cp.iface.enp11s0f0.addr"))
    check(kv.get("cp.iface.enp11s0f0.mtu") == "9000",
          "mtu: %r" % kv.get("cp.iface.enp11s0f0.mtu"))
    check(kv.get("dp.l3.iface.1.mac") == "02:11:22:33:44:55",
          "mac must be keyed by PORT INDEX: %r" % kv.get("dp.l3.iface.1.mac"))
    check(not any(k.startswith("dp.l3.iface.enp") for k in kv),
          "a MAC was published under an interface NAME; the DP cannot parse it")
    # ethernet1/9 has no alias -> must not be emitted under either name.
    check(not any("ethernet1/9" in k or "1/9" in k for k in kv),
          "unaliased interface leaked into the config")
    check(kv.get("cp.iface.count") == "3", "iface count: %r" % kv.get("cp.iface.count"))

    # -- routers: only admin_up ones ---------------------------------------
    check(kv.get("cp.vr.1.name") == "default", "vr1 name")
    check("cp.vr.2.name" not in kv, "admin-down VR should be skipped")
    check(kv.get("cp.vr.count") == "1", "vr count: %r" % kv.get("cp.vr.count"))

    # -- policy: summary only, never the rules -----------------------------
    check(kv.get("dp.policy.rules") == "2",
          "enabled+visible rules: %r" % kv.get("dp.policy.rules"))
    check(kv.get("dp.policy.zerotier_rules") == "1",
          "zerotier rule count: %r" % kv.get("dp.policy.zerotier_rules"))
    check(not any(".name" in k and "iface" not in k and "vr" not in k for k in kv),
          "a rule name leaked into the rendered config")
    check(all("src_ip" not in k and "dst_ip" not in k for k in kv),
          "rule match fields leaked into the rendered config")

    # -- the merge preserves what the manager does not model ---------------
    check(kv.get("cp.bcm.vlan1.ports") == "xe5,xe8",
          "curated BCM key was lost in the merge")
    check(kv.get("dp.session.table.size") == "1048576",
          "curated DP key was lost in the merge")
    check(kv.get("all.platform") == "pa5220",
          "base platform must win; the renderer must not assert pa5200: %r"
          % kv.get("all.platform"))

    # -- an unmapped interface is skipped LOUDLY, never emitted by name -----
    lines_np, problems_np = R.render(dbp, xmlp, os.devnull)
    kv_np = dict(l.split("=", 1) for l in lines_np)
    check(not any(k.startswith("dp.l3.route.") for k in kv_np),
          "routes were published with no portmap at all")
    check(any("portmap" in p for p in problems_np),
          "missing portmap was not reported: %r" % (problems_np,))

    # -- the version must be stable, and move only on a real change --------
    v1 = kv["dp.policy.version"]
    again = dict(l.split("=", 1) for l in R.render(dbp, xmlp, localp)[0])
    check(again["dp.policy.version"] == v1,
          "version is not deterministic across renders")
    db = sqlite3.connect(dbp)
    db.execute("UPDATE policy_rules SET action='deny' WHERE id=2")
    db.commit(); db.close()
    v2 = dict(l.split("=", 1) for l in R.render(dbp, xmlp, localp)[0])["dp.policy.version"]
    check(v2 != v1, "version did not change when a rule changed")

    # -- secrets never render ---------------------------------------------
    out = {}
    for k in ("cp.zerotier.secret", "cp.wifi.password", "dp.api_token",
              "cp.identity.private", "cp.psk"):
        R._emit(out, k, "SHOULD-NOT-APPEAR")
    check(not out, "secret-shaped keys were emitted: %r" % out)
    # ...but a legitimate key that merely contains a denied substring as part of
    # a longer word must still be caught -- the denylist is intentionally broad.
    R._emit(out, "cp.monkey.value", "x")
    check("cp.monkey.value" not in out,
          "denylist should be substring-based (monKEY) -- documented as broad")

    # -- a newline in a value is refused, not escaped -----------------------
    try:
        R._emit({}, "cp.x", "line1\nline2=evil")
        fails.append("newline value was accepted")
    except R.RenderError:
        pass

    # -- write_if_changed: only writes on real change ----------------------
    outp = os.path.join(tmp, "config.env")
    check(R.write_if_changed(outp, lines) is True, "first write should happen")
    check(R.write_if_changed(outp, lines) is False,
          "identical content must NOT rewrite (it would bump cfgd's version "
          "and force a needless DP push)")

    # -- deterministic ordering -------------------------------------------
    check(lines == sorted(lines), "output is not sorted; version would churn")

    if fails:
        print("FAIL (%d)" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("ok: %d keys rendered, all domains correct" % len(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
