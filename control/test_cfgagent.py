#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Unit-test ffn_cfgagent's DP convergence, especially "does the DP still hold it".

The interesting logic here is not the happy path. It is the two ways the agent
can be silently wrong about the dataplane:

  * re-pushing forever over a SINGLE-SESSION mailbox that other things need, or
  * believing the DP holds config that the DP lost in a reboot.

The second one is real and was observed live. The DP's /etc/ffn is in its
initramfs, which is tmpfs, so a DP reboot destroys the pushed config while
.dp.pushed on the CP survives on the CP's NFS root. After the DP was re-rooted
over NFS, /etc/ffn did not exist on the DP in either root and the CP's marker
still matched -- so nothing was ever pushed again and nothing reported it.

Everything is driven through a fake ffn-dpsh, so no appliance is touched.

Run: python3 test_cfgagent.py
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_cfgagent as A  # noqa: E402

BODY = "a=1\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Redirect every path the module writes so the real /etc/ffn is safe.
        self._saved = {k: getattr(A, k) for k in
                       ("DP_PUSHED", "DP_PUSHED_AT", "DPSH", "DP_CONF", "CP_CONF")}
        A.DP_PUSHED = os.path.join(self.tmp, ".dp.pushed")
        A.DP_PUSHED_AT = os.path.join(self.tmp, ".dp.pushed.at")
        A.DPSH = os.path.join(self.tmp, "fake-dpsh")
        A.DP_CONF = os.path.join(self.tmp, "dp.env")
        A.CP_CONF = os.path.join(self.tmp, "cp.env")
        A._dp_last_verify[0] = 0.0
        self.calls = []
        self.dp_bytes = None          # None = unreachable; -1 = file absent

        def fake_capture(cmd, timeout="30"):
            self.calls.append(cmd)
            if self.dp_bytes is None:
                return None
            # ffn-dpsh ECHOES the command before the reply. Reproducing that
            # here is the point: a check that accidentally matches its own
            # command text would pass without the echo present to catch it.
            return "%s\n%d\n" % (cmd, self.dp_bytes)

        self._real_capture = A._dpsh_capture
        A._dpsh_capture = fake_capture

    def tearDown(self):
        A._dpsh_capture = self._real_capture
        for k, v in self._saved.items():
            setattr(A, k, v)

    def mark(self, body, age=3600):
        """Pretend a push of `body` was confirmed `age` seconds ago."""
        with open(A.DP_PUSHED, "w") as fh:
            fh.write(body)
        with open(A.DP_PUSHED_AT, "w") as fh:
            fh.write("%.0f\n" % (time.time() - age))


class TestDpHolds(Base):
    def test_matching_size_means_it_holds(self):
        self.dp_bytes = len(BODY)
        self.assertIs(A.dp_holds(BODY), True)

    def test_absent_file_reports_false(self):
        self.dp_bytes = -1                     # the `expr 0 - 1` arm
        self.assertIs(A.dp_holds(BODY), False)

    def test_truncated_push_reports_false(self):
        """The mailbox truncates; a short file is a real failure mode here."""
        self.dp_bytes = 2
        self.assertIs(A.dp_holds(BODY), False)

    def test_unreachable_is_none_not_false(self):
        """None and False are different answers and must stay different.

        False means "the DP demonstrably lost it" and triggers a re-push.
        None means "no idea"; re-pushing on that would hammer a shared
        single-session mailbox for as long as the DP is down.
        """
        self.dp_bytes = None
        self.assertIsNone(A.dp_holds(BODY))

    def test_does_not_match_the_command_echo(self):
        """The number must come from the DP, not from our own command text.

        ffn-dpsh echoes the command, so a check whose marker appears in that
        command matches the echo -- exactly the bug that made the unwedge tool
        call a wedged shell healthy. A line holding nothing but an integer
        cannot be the echo of a command that contains words.
        """
        A._dpsh_capture = lambda cmd, timeout="30": cmd + "\n"
        self.assertIsNone(A.dp_holds(BODY))


class TestLostConfigDetection(Base):
    def test_absent_on_the_dp_is_detected(self):
        self.mark(BODY)
        self.dp_bytes = -1
        self.assertTrue(A.dp_lost_config_since_push())

    def test_present_on_the_dp_is_left_alone(self):
        self.mark(BODY)
        self.dp_bytes = len(BODY)
        self.assertFalse(A.dp_lost_config_since_push())

    def test_unreachable_dp_does_not_churn(self):
        """No answer is not evidence of loss."""
        self.mark(BODY)
        self.dp_bytes = None
        self.assertFalse(A.dp_lost_config_since_push())

    def test_no_marker_means_nothing_to_verify(self):
        self.dp_bytes = -1
        self.assertFalse(A.dp_lost_config_since_push())
        self.assertEqual(self.calls, [], "nothing confirmed -> nothing to check")

    def test_rate_limited(self):
        self.mark(BODY)
        self.dp_bytes = len(BODY)
        A.dp_lost_config_since_push()
        n = len(self.calls)
        A.dp_lost_config_since_push()
        self.assertEqual(len(self.calls), n,
                         "second check inside the interval must not probe")

    def test_heals_a_marker_written_by_the_older_agent(self):
        """The transition case: a confirmation marker with no timestamp at all.

        An earlier draft compared the DP's uptime against the time since the
        push. That cannot answer here -- there is nothing to compare against --
        so it would have left this exact stale marker in place forever, which
        is the state the change had to heal. Asking the DP needs no history.
        """
        with open(A.DP_PUSHED, "w") as fh:
            fh.write(BODY)
        self.assertFalse(os.path.exists(A.DP_PUSHED_AT))
        self.dp_bytes = -1
        self.assertTrue(A.dp_lost_config_since_push())


class TestPushGating(Base):
    def setUp(self):
        Base.setUp(self)
        self.pushed = []
        A.push_to_dp = lambda lines, verbose=False: (
            self.pushed.append(list(lines)) or True)

    def tearDown(self):
        A.push_to_dp = A.__dict__.get("_real_push", A.push_to_dp)
        Base.tearDown(self)

    def test_unchanged_and_dp_holds_it_skips(self):
        self.mark(BODY)
        self.dp_bytes = len(BODY)
        self.assertTrue(A.push_to_dp_if_needed(["a=1"]))
        self.assertEqual(self.pushed, [], "no change and DP holds it -> no push")

    def test_unchanged_but_dp_lost_it_repushes(self):
        """The regression this whole change exists for."""
        self.mark(BODY)
        self.dp_bytes = -1
        self.assertTrue(A.push_to_dp_if_needed(["a=1"]))
        self.assertEqual(self.pushed, [["a=1"]], "DP lost it -> must re-push")

    def test_changed_content_always_pushes(self):
        self.mark(BODY)
        self.dp_bytes = len(BODY)
        self.assertTrue(A.push_to_dp_if_needed(["a=2"]))
        self.assertEqual(self.pushed, [["a=2"]])

    def test_failed_push_is_retried_next_cycle(self):
        """A failed push must not be recorded as delivered."""
        A.push_to_dp = lambda lines, verbose=False: False
        self.assertFalse(A.push_to_dp_if_needed(["a=1"]))
        self.assertFalse(os.path.exists(A.DP_PUSHED),
                         "a failed push must leave no confirmation marker")

    def test_successful_push_writes_both_files(self):
        self.assertTrue(A.push_to_dp_if_needed(["a=1"]))
        self.assertTrue(os.path.exists(A.DP_PUSHED))
        self.assertTrue(os.path.exists(A.DP_PUSHED_AT))
        with open(A.DP_PUSHED) as fh:
            self.assertEqual(fh.read(), BODY)

    def test_timestamp_alone_confirms_nothing(self):
        with open(A.DP_PUSHED_AT, "w") as fh:
            fh.write("%.0f\n" % (time.time() - 3600))
        self.dp_bytes = len(BODY)
        self.assertTrue(A.push_to_dp_if_needed(["a=1"]))
        self.assertEqual(self.pushed, [["a=1"]])


class TestChunking(Base):
    """The mailbox is not a bulk transport; the chunk sizes are load-bearing."""

    def test_chunks_respect_both_limits(self):
        cmds = ["echo '%s' >> /etc/ffn/dp.env.tmp" % ("k%d=v%d" % (i, i))
                for i in range(40)]
        batches = list(A._chunk(cmds))
        self.assertTrue(batches)
        for b in batches:
            self.assertLessEqual(len(b), A.DP_PUSH_CHUNK_CMDS)
            size = sum(len(c) + 2 for c in b)
            # A single oversized command cannot be split, so the byte limit
            # only binds once a batch holds more than one.
            if len(b) > 1:
                self.assertLessEqual(size, A.DP_PUSH_CHUNK_BYTES)
        self.assertEqual([c for b in batches for c in b], cmds,
                         "chunking must not drop or reorder commands")


if __name__ == "__main__":
    unittest.main(verbosity=2)
