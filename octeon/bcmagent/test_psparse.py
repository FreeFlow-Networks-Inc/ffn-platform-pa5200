#!/usr/bin/env python3
"""Unit-test ffn-bcmd's `ps` parser against REAL captured hardware output.

Every line in FIXTURE below was copied verbatim out of a bcm.user transcript on
the PA-5220 (2026-09-04), not hand-written -- the same principle as
octeon/tests/extract.py, which exists so its test exercises real shipped source
rather than a retyped copy. A parser tested only against invented input tells
you nothing about the table the chip actually prints.

The fixture is chosen to cover every shape the real table produces:

  * !ena           administratively disabled (how the shipped config leaves
                   every front-panel port)
  * up / down      the two link states
  * '-' speed      a port with no speed resolved (xe8/xe9 sit in autoneg)
  * 12.5G          a non-integer speed (the ILKN port) -- this is why the
                   speed parser handles a decimal point
  * MAC / PHY      a loopback suffix in the trailing columns, which must NOT be
                   mistaken for part of the fields we read
  * 100G / 40G     wider speeds, and ce3's 'Forward' STP state where every other
                   row says 'Disable'

Run: python3 test_psparse.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_bcmd  # noqa: E402

# Verbatim from a real `ps` on the appliance. Trailing spaces preserved.
FIXTURE = """\
                 ena/    speed/ link auto    STP                  lrn  inter   max  loop
           port  link    duplex scan neg?   state   pause  discrd ops   face frame  back
       xe1(  1)  !ena   10G  FD   SW  No   Disable  TX RX   None    D    XFI 16360
       xl2(  2)  up     40G  FD   SW  No   Disable  TX RX   None    D  XLAUI 16360  PHY
       ce3(  3)  down  100G  FD   SW  No   Forward     RX   None   FA   CAUI 16360
       xe8(  8)  down     -       SW  Yes  Disable  TX RX   None    D     KR 16360
      xe13( 13)  up     10G  FD   SW  No   Disable  TX RX   None    D    XFI 16360  MAC
      il20( 20)  down 12.5G  FD None  No   Disable          None    D   ILKN     0
      ce32( 32)  !ena  100G  FD   SW  No   Disable  TX RX   None    D   CAUI 16360
"""

EXPECT = {
    # port: (name, enabled, link, speed_mb, faceplate)
    1:  ("xe1",  False, False, 10000,  True),
    2:  ("xl2",  True,  True,  40000,  False),   # internal DP link, not faceplate
    3:  ("ce3",  True,  False, 100000, False),
    8:  ("xe8",  True,  False, None,   False),   # '-' speed, and not in the FP list
    13: ("xe13", True,  True,  10000,  True),
    20: ("il20", True,  False, 12500,  False),   # 12.5G must not truncate to 12000
    32: ("ce32", False, False, 100000, True),
}


class FakeChip(object):
    """Stands in for the pty session: op_port_list only calls chip.run("ps")."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def run(self, cmd, timeout=None):
        self.calls.append(cmd)
        return self.text


def main():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    chip = FakeChip(FIXTURE)
    out = ffn_bcmd.op_port_list(chip, {})

    check(chip.calls == ["ps"], "expected exactly one `ps` call, got %r" % chip.calls)
    check(out["count"] == len(EXPECT),
          "parsed %d rows, expected %d -- a header line may be matching"
          % (out["count"], len(EXPECT)))

    got = {p["port"]: p for p in out["ports"]}
    for port, (name, ena, link, speed, fp) in EXPECT.items():
        p = got.get(port)
        if p is None:
            fails.append("port %d missing from parse" % port)
            continue
        check(p["name"] == name, "port %d name %r != %r" % (port, p["name"], name))
        check(p["enabled"] == ena, "port %d enabled %r != %r" % (port, p["enabled"], ena))
        check(p["link"] == link, "port %d link %r != %r" % (port, p["link"], link))
        check(p["speed_mb"] == speed,
              "port %d speed_mb %r != %r" % (port, p["speed_mb"], speed))
        check(p["faceplate"] == fp,
              "port %d faceplate %r != %r" % (port, p["faceplate"], fp))

    # The two header lines must not parse as ports. They are the reason the row
    # regex anchors on "name( N)" rather than just splitting on whitespace.
    check(all(p["name"] not in ("port", "link") for p in out["ports"]),
          "a header line was parsed as a port")

    # An empty/garbage table must raise rather than silently report zero ports:
    # "no ports" from a live switch is a bug, not a valid answer.
    try:
        ffn_bcmd.op_port_list(FakeChip("nothing useful here\n"), {})
        fails.append("empty table did not raise")
    except RuntimeError:
        pass

    # Speed helper edge cases.
    for tok, want in (("10G", 10000), ("12.5G", 12500), ("100G", 100000),
                      ("-", None), ("", None), ("1000M", 1000), ("junk", None)):
        check(ffn_bcmd._speed_mb(tok) == want,
              "_speed_mb(%r) = %r, want %r" % (tok, ffn_bcmd._speed_mb(tok), want))

    # _strip must drop the echoed command and the trailing prompt, and nothing
    # else -- a reply that loses its first line looks like an empty result.
    stripped = ffn_bcmd.Chip._strip("ps\r\nrow one\r\nrow two\r\nBCM.0> ", "ps")
    check(stripped == "row one\nrow two",
          "_strip returned %r" % (stripped,))

    # The faceplate list is written down TWICE: here in ffn_bcmd.FACEPLATE, so
    # port.list can mark faceplate ports without shelling out, and again in
    # ffn_bcmports.VENDOR_FRONT_PANEL, which is the port table proper. Two
    # copies of a hardware fact drift, and the one that drifts is always the
    # copy nobody is looking at -- the daemon would then either hide a port
    # that can be enabled or offer one that cannot. Nothing enforced their
    # agreement, so this does.
    try:
        import os as _os
        _here = _os.path.dirname(_os.path.abspath(__file__))
        sys.path.insert(0, _os.path.normpath(_os.path.join(_here, "..", "..")))
        import ffn_bcmports
    except ImportError:
        ffn_bcmports = None
        print("  (skipped: ffn_bcmports not importable from here)")
    if ffn_bcmports is not None:
        check(sorted(ffn_bcmd.FACEPLATE) == sorted(ffn_bcmports.VENDOR_FRONT_PANEL),
              "ffn_bcmd.FACEPLATE and ffn_bcmports.VENDOR_FRONT_PANEL disagree: %r"
              % (set(ffn_bcmd.FACEPLATE) ^ set(ffn_bcmports.VENDOR_FRONT_PANEL),))
        # And every faceplate port must have a connector position, or the WebUI
        # has a port it can list but cannot name.
        check(sorted(ffn_bcmports.FACEPLATE) == sorted(ffn_bcmd.FACEPLATE),
              "the faceplate POSITION map does not cover the same ports")
        names = [ffn_bcmports.pan_ifname(p) for p in ffn_bcmports.faceplate_ports()]
        check(len(set(names)) == len(names) and all(names),
              "faceplate ports do not map to distinct interface names")

    # The PCI inventory op must not depend on the chip session: it is the one
    # query that has to answer while bcm.user is initialising or dead.
    import inspect as _inspect
    src = _inspect.getsource(ffn_bcmd.op_sys_inventory)
    check("chip." not in src,
          "op_sys_inventory touches the chip session; it must read sysfs only")

    if fails:
        print("FAIL (%d)" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("ok: %d ports parsed, all fields correct" % out["count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
