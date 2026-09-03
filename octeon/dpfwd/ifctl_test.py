#!/usr/bin/env python3
"""Full WebUI-to-dataplane path, exercised against the real C DP.

dp_serve runs the actual dp_service_commands() loop over an mmap'd region, so
this tests the genuine wire format and the genuine handlers -- not a mock of
either. The chain under test is:

    plan (what configd would hold)
      -> ffn_ifctl.apply_all()      -> CMD ring
      -> real C DP dispatch          -> port table
      -> ffn_ifctl.ports()           -> the dicts the WebUI page renders
"""
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/opt/ffn-ngfw-v2")
import ffn_dpring as D
import ffn_ifctl

SERVE = "/opt/ffn-ngfw-v2/octeon-dp/dp_serve"
fails = []


def chk(c, m):
    print(("  ok   " if c else "  FAIL ") + m)
    if not c:
        fails.append(m)


region = os.path.join(tempfile.mkdtemp(), "region.bin")
with open(region, "wb") as f:
    f.write(bytes(D.OFF_BANK0 + 65536))

srv = subprocess.Popen([SERVE, region, "25"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
time.sleep(0.5)
REGION = "file:" + region

try:
    print("=== 1. reachability and capabilities ===")
    with ffn_ifctl.IfCtl(REGION) as c:
        st = c.caps()
        print("  " + json.dumps(st))
        chk(st["available"], "region reachable through ffn_ifctl")
        chk(st["port_ctl"], "DP advertises PORT_CTL")
        chk(st["hardware_applied"] is False,
            "PORT_HW is False -- no CVMX, so nothing is driven (honest)")
        chk(c.ports() == [], "port table starts empty")

    print()
    print("=== 2. apply the PA-5220 complement ===")
    plan = ([{"lport": i, "form_factor": "RJ45", "role": "data",
              "speed_mbps": 1000, "mtu": 1500} for i in range(2)] +
            [{"lport": 2 + i, "form_factor": "SFP+", "role": "data",
              "speed_mbps": 10000, "mtu": 9216} for i in range(8)] +
            [{"lport": 10 + i, "form_factor": "QSFP+", "role": "HSCI",
              "speed_mbps": 40000, "mtu": 9216} for i in range(2)])
    with ffn_ifctl.IfCtl(REGION) as c:
        r = c.apply_all(plan)
        print("  queued=%d confirmed=%d failed=%d" % (r["queued"], r["confirmed"], len(r["failed"])))
        chk(r["confirmed"] == 12, "all 12 ports CONFIRMED in the DP table")
        chk(not r["failed"], "no failures")

    print()
    print("=== 3. what the WebUI page would render ===")
    with ffn_ifctl.IfCtl(REGION) as c:
        ports = c.ports()
        chk(len(ports) == 12, "12 interfaces returned (%d)" % len(ports))
        # the shape the existing page already consumes
        need = {"name", "port", "type", "link_up", "speed_gbps", "mtu",
                "rx_packets", "tx_packets"}
        chk(need <= set(ports[0]),
            "entries carry the fields _discover_interfaces() already uses")
        chk(all(p["type"] == "octeon" for p in ports),
            "all marked type=octeon so the page can distinguish them")
        chk(ports[0]["name"] == "ethernet1/1",
            "named ethernet1/N like PAN (%s)" % ports[0]["name"])
        chk(ports[11]["name"] == "ethernet1/12", "last is ethernet1/12")

        rj = [p for p in ports if p["form_factor"] == "RJ45"]
        sfp = [p for p in ports if p["form_factor"] == "SFP+"]
        qsfp = [p for p in ports if p["form_factor"] == "QSFP+"]
        chk((len(rj), len(sfp), len(qsfp)) == (2, 8, 2),
            "2 RJ-45 / 8 SFP+ / 2 QSFP+ round-tripped")
        chk(sfp[0]["speed_gbps"] == 10.0 and qsfp[0]["speed_gbps"] == 40.0,
            "speeds render as 10.0 / 40.0 Gbps")
        chk(all(p["role"] == "HSCI" for p in qsfp),
            "the QSFP+ pair carries role HSCI")
        chk(all(not p["bridgeable"] for p in qsfp),
            "HSCI ports report bridgeable=False for the UI")
        chk(all(p["bridgeable"] for p in rj + sfp),
            "data ports report bridgeable=True")
        chk(all(p["state"] == "powerdown" for p in ports),
            "configuring alone leaves every port powerdown, not live")
        chk(all(p["hardware_applied"] is False for p in ports),
            "every entry flags hardware_applied=False")

        for p in ports[:3] + ports[-2:]:
            print("    %-13s %-6s %-5s %-9s %s"
                  % (p["name"], p["form_factor"], p["role"], p["state"],
                     p["lmac"]))

    print()
    print("=== 4. admin-up: data yes, HSCI refused ===")
    with ffn_ifctl.IfCtl(REGION) as c:
        r = c.apply_port(2, "SFP+", role="data", speed_mbps=10000,
                         mtu=9216, admin_up=True)
        chk(r.get("ok"), "a data port accepts admin-up")
        r = c.apply_port(10, "QSFP+", role="HSCI", speed_mbps=40000,
                         mtu=9216, admin_up=True)
        chk(not r.get("ok") and "not bridgeable" in r.get("error", ""),
            "an HSCI port is refused admin-up before it reaches the DP")
        time.sleep(0.3)
        by = {p["port"]: p for p in c.ports()}
        chk(by[2]["admin_up"] is True, "port 2 admin_up recorded by the DP")
        chk(by[10]["admin_up"] is False, "port 10 stayed down")
        chk(by[2]["state"] == "startup",
            "port 2 sits at startup, not run -- no hardware behind it")

    print()
    print("=== 5. a bad region is reported, not guessed at ===")
    with ffn_ifctl.IfCtl("file:/nonexistent/region.bin") as c:
        chk(not c.ok and c.error, "a missing region reports an error")
    with ffn_ifctl.IfCtl("nonsense") as c:
        chk(not c.ok and "file:" in (c.error or ""),
            "a malformed region string is rejected with guidance")
finally:
    try:
        srv.wait(timeout=30)
    except subprocess.TimeoutExpired:
        srv.kill()

print()
print("==== WebUI -> DP interface path: %d failed ====" % len(fails))
sys.exit(1 if fails else 0)
