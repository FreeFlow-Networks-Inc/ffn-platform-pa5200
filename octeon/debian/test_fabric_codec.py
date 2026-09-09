import struct
import unittest
from ffn_fabric import decode_cmh, encode_itmh, record, unpack, MAX_FRAME


class FabricCodec(unittest.TestCase):
    def test_owner_header_and_padding(self):
        frame = bytes(range(60))
        cmh = bytearray(32)
        cmh[0], cmh[3] = 0x10, 3
        struct.pack_into('!HH', cmh, 24, 13 << 6, len(frame))
        self.assertEqual(decode_cmh(bytes(cmh) + frame + b'padding'), (13, frame))
        cmh[23] = 1
        self.assertIsNone(decode_cmh(bytes(cmh) + frame))

    def test_truncated_and_unsupported_packets(self):
        for length in range(46):
            self.assertIsNone(decode_cmh(bytes(length)))
        for port in (0, 2, 24):
            with self.assertRaises(ValueError):
                encode_itmh(port, bytes(60))
        with self.assertRaises(ValueError):
            record(1, bytes(MAX_FRAME + 1))

    def test_split_stream_and_multiple_frames(self):
        expected = [(1, bytes(range(60))), (13, bytes(1518))]
        wire = b''.join(record(p, f) for p, f in expected)
        for chunk in (1, 3, 64, 4096):
            buf, actual = bytearray(), []
            for offset in range(0, len(wire), chunk):
                buf.extend(wire[offset:offset+chunk])
                actual.extend(unpack(buf))
            self.assertEqual(actual, expected)
            self.assertFalse(buf)

    def test_direct_system_port_header(self):
        f = bytes(range(60))
        self.assertEqual(encode_itmh(5, f), b'\x01\x00\x10\x00' + f[:12] + bytes(8) + f[12:])

    def test_corrupt_stream_rejected(self):
        for h in (b'\x02\x01\x00\x3c', b'\x01\x02\x00\x3c', b'\x01\x01\xff\xff'):
            with self.assertRaises(ValueError):
                list(unpack(bytearray(h)))


if __name__ == '__main__':
    unittest.main()
