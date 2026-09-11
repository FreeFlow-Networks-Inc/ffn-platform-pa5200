#!/usr/bin/env python3
"""Run against the native or MIPS64 adapter; requires no raw sockets."""
import os
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ffn_inspection as inspection
from ffn_inspection import library, validate

LIB = library(os.environ.get('FFN_INLINE_LIB', '/usr/local/lib/libffn-inline.so'))


def packet(payload=b'FFN_TEST_DENY', version=4, tcp=False, tagged=False):
    l4 = (struct.pack('!HHIIHHHH', 1000, 2000, 0, 0, 0x5018, 4096, 0, 0) if tcp else
          struct.pack('!HHHH', 1000, 2000, len(payload)+8, 0)) + payload
    proto = 6 if tcp else 17
    h = (struct.pack('!BBHHHBBH4s4s', 0x45, 0, len(l4)+20, 0, 0, 64, proto, 0, b'\0'*4, b'\0'*4)
         if version == 4 else struct.pack('!IHBB', 0x60000000, len(l4), proto, 64)+b'\0'*32)
    eth = b'\0'*12 + (b'\x81\x00\x00\x64' if tagged else b'')
    return eth + (b'\x08\x00' if version == 4 else b'\x86\xdd') + h + l4


class Adapter(unittest.TestCase):
    def test_builtin_profiles(self):
        for detector, payload in [('credit_card', b'4111111111111111'), ('ssn', b'123-45-6789'), ('api_key', b'AKIAIOSFODNN7EXAMPLE')]:
            cfg = {'revision':0, 'mode':'block', 'ports':[1], 'literal':'', 'detectors':[detector]}
            validate(cfg)
            state = inspection.create_engine(LIB, cfg)
            self.assertTrue(state)
            try:
                self.assertEqual(LIB.ffn_inline_scan(state, packet(payload), len(packet(payload))), 2)
                self.assertEqual(LIB.ffn_inline_scan(state, packet(b'clean'), len(packet(b'clean'))), 0)
            finally:
                LIB.ffn_inline_destroy(state)

    def setUp(self):
        self.state = LIB.ffn_inline_create(b'FFN_TEST_DENY', 13, 2)
        self.assertTrue(self.state)

    def tearDown(self):
        LIB.ffn_inline_destroy(self.state)

    def scan(self, frame):
        return LIB.ffn_inline_scan(self.state, frame, len(frame))

    def test_protocols_and_tags(self):
        for version in (4, 6):
            for tcp in (False, True):
                for tagged in (False, True):
                    self.assertEqual(self.scan(packet(version=version, tcp=tcp, tagged=tagged)), 2)
                    self.assertEqual(self.scan(packet(b'clean', version, tcp, tagged)), 0)

    def test_padding_not_scanned(self):
        self.assertEqual(self.scan(packet(b'clean')+b'FFN_TEST_DENY'), 0)

    def test_fragments_and_extensions_explicitly_unsupported(self):
        f = bytearray(packet())
        f[20] = 0x20
        self.assertEqual(self.scan(bytes(f)), -2)
        f = bytearray(packet(version=6))
        f[20] = 44
        self.assertEqual(self.scan(bytes(f)), -2)

    def test_truncated_and_bad_lengths(self):
        f = packet()
        for length in range(len(f)):
            self.assertLess(self.scan(f[:length]), 0)
        f = bytearray(packet())
        f[38:40] = b'\xff\xff'
        self.assertEqual(self.scan(bytes(f)), -1)
        f = bytearray(packet(tcp=True))
        f[46] = 0xf0
        self.assertEqual(self.scan(bytes(f)), -1)

    def test_alert_and_constructor_validation(self):
        self.assertFalse(LIB.ffn_inline_create(b'', 0, 2))
        self.assertFalse(LIB.ffn_inline_create(b'x'*64, 64, 2))
        self.assertFalse(LIB.ffn_inline_create(b'x', 1, 3))
        h = LIB.ffn_inline_create(b'FFN_TEST_DENY', 13, 1)
        try:
            f = packet()
            self.assertEqual(LIB.ffn_inline_scan(h, f, len(f)), 1)
        finally:
            LIB.ffn_inline_destroy(h)

    def test_random_input_bounds(self):
        rng = random.Random(5220)
        for _ in range(10000):
            f = bytes(rng.getrandbits(8) for _ in range(rng.randrange(1600)))
            self.assertIn(self.scan(f), (-2, -1, 0, 1, 2))

    def test_policy_rejects_invalid(self):
        good = {'revision': 0, 'mode': 'block', 'ports': [1], 'literal': 'test'}
        self.assertEqual(validate(good), good)
        for key, value in [('ports', [23]), ('ports', [True]), ('ports', [1, 1]),
                           ('literal', ''), ('literal', 'x'*64), ('revision', True), ('mode', 'reset')]:
            with self.assertRaises(ValueError):
                validate(dict(good, **{key: value}))

    def test_reload_scope_and_keep_last_valid_policy(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            policy = Path(directory)/'policy.json'
            status = Path(directory)/'status.json'
            with patch.object(inspection, 'POLICY', policy), patch.object(inspection, 'STATUS', status), \
                    patch.object(inspection, 'library', return_value=LIB):
                inspector = inspection.Inspector()
                try:
                    self.assertTrue(inspector.allow(1, packet()))
                    policy.write_text(json.dumps({'revision': 1, 'mode': 'block', 'ports': [1], 'literal': 'FFN_TEST_DENY'}))
                    inspector.next_poll = 0
                    inspector.tick()
                    self.assertFalse(inspector.allow(1, packet()))
                    self.assertTrue(inspector.allow(3, packet()))
                    policy.write_text('{invalid')
                    inspector.next_poll = 0
                    inspector.tick()
                    self.assertIsNotNone(inspector.error)
                    self.assertFalse(inspector.allow(1, packet()))
                    policy.write_text(json.dumps({'revision': 2, 'mode': 'off', 'ports': [], 'literal': ''}))
                    inspector.next_poll = 0
                    inspector.tick()
                    self.assertIsNone(inspector.error)
                    self.assertTrue(inspector.allow(1, packet()))
                    self.assertEqual(json.loads(status.read_text())['revision'], 2)
                finally:
                    inspector.close()
                self.assertFalse(status.exists())


if __name__ == '__main__':
    unittest.main()
