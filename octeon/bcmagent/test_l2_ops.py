#!/usr/bin/env python3
"""Tests for ffn-bcmd's VLAN / STP / trunk ops -- no hardware required.

These ops exist to fill the gap control/apply.d/50-fabric documents: it renders
dp.fabric.vlan.* and dp.fabric.stp.forward and then reports them UNAPPLIED,
because ffn-bcmd had only port enable and loopback.

WHAT CAN AND CANNOT BE TESTED HERE. The BCM API's behaviour cannot -- that
needs the chip. What CAN be tested, and is what actually went wrong while
writing these, is the cint text the ops generate and the decisions they make
around it: argument validation, port-name parsing, idempotence, and whether a
non-zero status is turned into an exception or swallowed. A fake chip records
the command and replies with a canned status, so every branch is reachable.

The first version of these ops emitted `' + NL + '` into the generated source
and raised NameError on the first call -- a fake chip catches that instantly
where hardware would have cost a re-init to find out.
"""
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "ffn_bcmd_under_test", os.path.join(HERE, "ffn_bcmd.py"))
bcmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcmd)


class FakeChip:
    """Records commands; replies with a canned status."""

    def __init__(self, reply="FFNRV 0\n"):
        self.cmds = []
        self.reply = reply

    def run(self, cmd, timeout=None):
        self.cmds.append(cmd)
        return self.reply

    @property
    def last(self):
        return self.cmds[-1]


class PortParsing(unittest.TestCase):
    def test_accepts_names_numbers_and_strings(self):
        f = bcmd._ports_arg
        self.assertEqual(f({"ports": [5, 8]}), [5, 8])
        self.assertEqual(f({"ports": "xe5,xe8,xl24"}), [5, 8, 24])
        self.assertEqual(f({"ports": "5, 8"}), [5, 8])
        self.assertEqual(f({"ports": 5}), [5])
        self.assertEqual(f({"ports": ["ce32", 33]}), [32, 33])
        self.assertEqual(f({}), [])

    def test_bad_port_name_is_an_error_not_a_silent_zero(self):
        with self.assertRaises(ValueError):
            bcmd._ports_arg({"ports": "eth0"})


class VlanCreate(unittest.TestCase):
    def test_generates_the_api_call(self):
        c = FakeChip()
        r = bcmd.op_vlan_create(c, {"vid": 100})
        self.assertIn("bcm_vlan_create(0, 100)", c.last)
        self.assertTrue(r["created"])

    def test_already_exists_is_success(self):
        # An applier re-runs on every commit. Treating "already there" as a
        # failure would make a converged configuration look broken.
        c = FakeChip("FFNRV -8\n")
        r = bcmd.op_vlan_create(c, {"vid": 100})
        self.assertFalse(r["created"])
        self.assertTrue(r["existed"])

    def test_other_errors_raise(self):
        c = FakeChip("FFNRV -4\n")
        with self.assertRaises(RuntimeError):
            bcmd.op_vlan_create(c, {"vid": 100})

    def test_vid_range_is_checked_before_touching_the_chip(self):
        c = FakeChip()
        for bad in (0, 4095, -1):
            with self.assertRaises(ValueError):
                bcmd.op_vlan_create(c, {"vid": bad})
        self.assertEqual(c.cmds, [], "must not reach the chip with a bad vid")

    def test_missing_status_is_distinguished_from_a_bad_status(self):
        c = FakeChip("cint exited\n")
        with self.assertRaises(RuntimeError) as e:
            bcmd.op_vlan_create(c, {"vid": 100})
        self.assertIn("no status", str(e.exception))


class VlanDestroy(unittest.TestCase):
    def test_refuses_vlan_1(self):
        c = FakeChip()
        with self.assertRaises(ValueError):
            bcmd.op_vlan_destroy(c, {"vid": 1})
        self.assertEqual(c.cmds, [])

    def test_not_found_is_success(self):
        c = FakeChip("FFNRV -7\n")
        r = bcmd.op_vlan_destroy(c, {"vid": 100})
        self.assertTrue(r["absent"])


class VlanPortAdd(unittest.TestCase):
    def test_builds_both_bitmaps(self):
        c = FakeChip()
        bcmd.op_vlan_port_add(c, {"vid": 100, "ports": "xe5,xe8"})
        for frag in ("BCM_PBMP_CLEAR(pbmp)", "BCM_PBMP_PORT_ADD(pbmp, 5)",
                     "BCM_PBMP_PORT_ADD(pbmp, 8)", "BCM_PBMP_CLEAR(ubmp)",
                     "bcm_vlan_port_add(0, 100, pbmp, ubmp)"):
            self.assertIn(frag, c.last)

    def test_untagged_defaults_to_every_member(self):
        # The flat domain this chip has always used is untagged; making the
        # caller spell that out every time invites a tagged-by-accident VLAN.
        c = FakeChip()
        r = bcmd.op_vlan_port_add(c, {"vid": 100, "ports": [5, 8]})
        self.assertEqual(r["untagged"], [5, 8])

    def test_explicit_empty_untagged_gives_a_tagged_trunk(self):
        c = FakeChip()
        r = bcmd.op_vlan_port_add(c, {"vid": 100, "ports": [5, 8], "untagged": []})
        self.assertEqual(r["untagged"], [])
        self.assertIn("BCM_PBMP_CLEAR(ubmp);", c.last)
        self.assertNotIn("BCM_PBMP_PORT_ADD(ubmp", c.last)

    def test_untagged_must_be_a_subset_of_members(self):
        c = FakeChip()
        with self.assertRaises(ValueError):
            bcmd.op_vlan_port_add(c, {"vid": 100, "ports": [5], "untagged": [8]})
        self.assertEqual(c.cmds, [])

    def test_ports_required(self):
        with self.assertRaises(ValueError):
            bcmd.op_vlan_port_add(FakeChip(), {"vid": 100})


class StpSet(unittest.TestCase):
    def test_one_call_per_port(self):
        c = FakeChip()
        r = bcmd.op_stp_set(c, {"ports": [5, 8, 24], "state": "forward"})
        self.assertEqual(len(c.cmds), 3)
        self.assertEqual(r["set"], [5, 8, 24])
        self.assertIn("BCM_STG_STP_FORWARD", c.cmds[0])

    def test_every_state_maps_to_a_bcm_constant(self):
        for name, const in bcmd._STP_STATES.items():
            c = FakeChip()
            bcmd.op_stp_set(c, {"port": 8, "state": name})
            self.assertIn(const, c.last)

    def test_bad_state_rejected(self):
        with self.assertRaises(ValueError):
            bcmd.op_stp_set(FakeChip(), {"port": 8, "state": "forwarding"})

    def test_total_failure_explains_the_config_gate(self):
        # -18 is BCM_E_PORT: the port is not in the Ethernet class. It is a
        # config-and-re-init fix, not a retry, and the message has to say so --
        # this exact code cost a long detour once already.
        c = FakeChip("FFNRV -18\n")
        with self.assertRaises(RuntimeError) as e:
            bcmd.op_stp_set(c, {"ports": [5, 8], "state": "forward"})
        msg = str(e.exception)
        self.assertIn("tm_port_header_type", msg)
        self.assertIn("re-init", msg)

    def test_partial_failure_is_reported_not_raised(self):
        # If some ports took the state and others did not, the caller needs
        # the split -- raising would hide the ports that succeeded.
        class Mixed(FakeChip):
            def run(self, cmd, timeout=None):
                self.cmds.append(cmd)
                return "FFNRV 0\n" if "0, 8," in cmd else "FFNRV -18\n"
        c = Mixed()
        r = bcmd.op_stp_set(c, {"ports": [5, 8], "state": "forward"})
        self.assertEqual(r["set"], [8])
        self.assertEqual([f["port"] for f in r["failed"]], [5])
        self.assertIn("Ethernet class", r["failed"][0]["meaning"])


class Trunk(unittest.TestCase):
    def test_builds_members_and_calls_set(self):
        c = FakeChip("FFNTC 0\nFFNRV 0\n")
        r = bcmd.op_trunk_create(c, {"tid": 1, "ports": [8, 9]})
        self.assertIn("bcm_trunk_member_t_init(&mem[0])", c.last)
        self.assertIn("mem[1].gport = 9", c.last)
        self.assertIn("bcm_trunk_create_id(0, 0, 1)", c.last)
        self.assertIn("bcm_trunk_set(0, 1, &ti, 2, mem)", c.last)
        self.assertEqual(r["members"], [8, 9])

    def test_member_count_bounds(self):
        for ports in ([8], list(range(1, 10))):
            with self.assertRaises(ValueError):
                bcmd.op_trunk_create(FakeChip(), {"tid": 1, "ports": ports})

    def test_set_failure_raises_and_reports_both_statuses(self):
        c = FakeChip("FFNTC 0\nFFNRV -4\n")
        with self.assertRaises(RuntimeError) as e:
            bcmd.op_trunk_create(c, {"tid": 1, "ports": [8, 9]})
        self.assertIn("bcm_trunk_set", str(e.exception))

    def test_create_exists_is_tolerated_when_set_succeeds(self):
        # Re-running an applier must converge: the trunk already existing is
        # not a failure as long as the membership set takes.
        c = FakeChip("FFNTC -8\nFFNRV 0\n")
        r = bcmd.op_trunk_create(c, {"tid": 1, "ports": [8, 9]})
        self.assertEqual(r["create_rv"], -8)
        self.assertEqual(r["set_rv"], 0)


class VlanList(unittest.TestCase):
    # This is REAL output, captured from the chip after programming VLAN 1.
    # The first version of the parser expected a line starting with the vid and
    # returned zero VLANs on a chip that had one -- "no VLANs" and "my regex
    # missed" are indistinguishable from the caller's side, so the fixture is
    # the actual bytes rather than something plausible.
    REAL = ("vlan 1\tports ce3,xl24,xe5,xe8 "
            "(0x0000000000000000000000000000000000000000000000000000000000"
            "000000000000000000000000000000000000000000000000000000000000"
            "0000000000000001000128), untagged ce3,xl24,xe5,xe8 "
            "(0x0000000000000000000000000000000000000000000000000000000000"
            "000000000000000000000000000000000000000000000000000000000000"
            "0000000000000001000128)\n")

    def test_parses_the_real_format(self):
        c = FakeChip(self.REAL)
        r = bcmd.op_vlan_list(c, {})
        self.assertEqual(r["count"], 1)
        v = r["vlans"][0]
        self.assertEqual(v["vid"], 1)
        self.assertEqual(v["ports"], ["ce3", "xl24", "xe5", "xe8"])
        self.assertEqual(v["untagged"], ["ce3", "xl24", "xe5", "xe8"])

    def test_hex_bitmaps_are_not_returned(self):
        # A 300-character constant in a JSON reply is noise; the names carry
        # the same fact.
        r = bcmd.op_vlan_list(FakeChip(self.REAL), {})
        self.assertNotIn("0x", json.dumps(r))

    def test_empty_output_is_zero_vlans_not_a_crash(self):
        r = bcmd.op_vlan_list(FakeChip(""), {})
        self.assertEqual(r["count"], 0)

    def test_tagged_trunk_shows_members_without_untagged(self):
        c = FakeChip("vlan 100\tports xe8,xe9 (0x1), untagged  (0x0)\n")
        r = bcmd.op_vlan_list(c, {})
        self.assertEqual(r["vlans"][0]["ports"], ["xe8", "xe9"])
        self.assertEqual(r["vlans"][0]["untagged"], [])


class ErrorNaming(unittest.TestCase):
    def test_18_is_named_for_what_it_actually_means(self):
        self.assertIn("Ethernet class", bcmd._bcm_err(-18))

    def test_unknown_code_does_not_crash(self):
        self.assertEqual(bcmd._bcm_err(-999), "?")


class Registration(unittest.TestCase):
    def test_all_new_ops_are_dispatchable(self):
        for op in ("vlan.create", "vlan.destroy", "vlan.port.add",
                   "vlan.port.remove", "vlan.list", "stp.set",
                   "trunk.create", "trunk.destroy"):
            self.assertIn(op, bcmd.OPS, "%s not registered" % op)


if __name__ == "__main__":
    unittest.main(verbosity=2)
