#!/usr/bin/env python3
"""The relay's DP read: a start-up allowance for the first message, the silence budget after it,
the committed configuration verified around every read, and generation boundaries logged once."""
import unittest

import session_relay


class Scripted:
    def __init__(self, messages):
        self.messages = list(messages); self.timeouts = []

    def read(self, timeout):
        self.timeouts.append(timeout)
        return self.messages.pop(0)


class DpReader(unittest.TestCase):
    def test_first_message_gets_the_start_up_allowance_and_later_ones_the_silence_budget(self):
        lines = Scripted([dict(operation='begin'), dict(operation='heartbeat'), dict(schema=1, unavailable='x'), dict(operation='begin')])
        verified = []; logged = []
        read = session_relay.dp_reader(lines, verify=lambda: verified.append(1), log=logged.append)
        self.assertEqual(read(), dict(operation='begin'))
        self.assertEqual(lines.timeouts, [session_relay.FIRST_MESSAGE_SECONDS])
        self.assertEqual(verified, [1, 1], 'the configuration is verified before and after the read')
        read(); read(); read()
        self.assertEqual(lines.timeouts[1:], [session_relay.SILENCE_SECONDS] * 3)
        # A generation end restarts the producer's own stream over the same connection: the receiver's
        # ten-second law between accepted messages applies there, so no second allowance is given.
        self.assertEqual(logged, ['DP generation started', 'DP generation ended: x', 'DP generation started'])
        self.assertGreater(session_relay.FIRST_MESSAGE_SECONDS, session_relay.SILENCE_SECONDS)

    def test_a_read_failure_leaves_the_allowance_spent(self):
        class Failing:
            calls = 0
            def read(self, timeout):
                self.calls += 1; self.timeouts = getattr(self, 'timeouts', []) + [timeout]
                raise TimeoutError('Session stream timed out')
        lines = Failing(); read = session_relay.dp_reader(lines, log=lambda text: None)
        with self.assertRaises(TimeoutError): read()
        with self.assertRaises(TimeoutError): read()
        self.assertEqual(lines.timeouts, [session_relay.FIRST_MESSAGE_SECONDS, session_relay.FIRST_MESSAGE_SECONDS],
                         'the allowance belongs to the connection, not to a successful read')


if __name__ == '__main__': unittest.main()
