import unittest
from ffn_packet_socket import otmh_program


def admitted(program, packet):
    accumulator = 0
    pc = 0
    while pc < len(program):
        code, yes, no, value = program[pc]
        if code == 0x28:
            if len(packet) < value + 2:
                return False
            accumulator = int.from_bytes(packet[value:value + 2], 'big')
        elif code == 0x15:
            pc += yes if accumulator == value else no
        elif code == 0x06:
            return bool(value)
        else:
            raise AssertionError('Unexpected BPF instruction')
        pc += 1
    raise AssertionError('BPF program did not return')


class AdmissionTests(unittest.TestCase):
    def test_all_source_ids_and_lag_aliases(self):
        sources = {28, 32, 36, 0x8001, 0x8101}
        program = otmh_program(sources)
        for source in range(65536):
            self.assertEqual(admitted(program, b'\x00\x18' + source.to_bytes(2, 'big')), source in sources)

    def test_wrong_destination_and_truncated_envelope(self):
        program = otmh_program([28])
        for packet in (b'', b'\0', b'\0\x18', b'\0\x18\0', b'\0\x19\0\x1c'):
            self.assertFalse(admitted(program, packet))

    def test_upper_bound_and_invalid_configuration(self):
        program = otmh_program(range(64))
        self.assertTrue(admitted(program, b'\0\x18\0\x3f'))
        self.assertFalse(admitted(program, b'\0\x18\0\x40'))
        for sources in ([], range(65), [-1], [65536], [True]):
            with self.assertRaises(ValueError):
                otmh_program(sources)


if __name__ == '__main__':
    unittest.main()
