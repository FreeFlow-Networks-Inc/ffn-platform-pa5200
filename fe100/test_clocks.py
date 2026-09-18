import unittest
from ffn_fe100_clocks import (apply_tcam, TCAM, TCAM_STATUS, TCAM_RESET,
                               TCAM_PLAN, RST, INIT, CLOCK_MASK, MONITORS)


class Registers:
    def __init__(self):
        self.values = dict(zip(TCAM, TCAM_RESET))
        self.values.update({RST:0x347e, INIT:0x800000, TCAM_STATUS:0})
        self.values.update(dict.fromkeys(MONITORS, 0))
        self.writes = []; self.lock = True; self.bad_write = None
    def read(self, register): return self.values[register]
    def write(self, register, value):
        self.writes.append((register, value))
        if self.bad_write == register:
            self.bad_write = None
            return
        self.values[register] = value
        if register == TCAM[0]:
            self.values[TCAM_STATUS] = int(not value & 1 and self.lock)
        if register in (RST, TCAM[0], *MONITORS):
            self.values[INIT] = 0x800000 | (CLOCK_MASK if
                self.values[TCAM_STATUS] & 1 and not self.values[RST] & 0x1a
                and all(self.values[r] & 1 for r in MONITORS) else 0)


class Clocks(unittest.TestCase):
    def test_verified_sequence(self):
        io = Registers()
        result = apply_tcam(io, TCAM_PLAN, sleep=lambda _:None)
        self.assertTrue(result['tcam_pll_locked'])
        self.assertEqual(result['init_status'] & CLOCK_MASK, CLOCK_MASK)
        self.assertFalse(result['session_offload_verified'])
        self.assertEqual(io.writes[-2:], [(RST, 0x347c), (RST, 0x3464)])
        self.assertEqual(set(r for r,v in io.writes), {*TCAM, RST, *MONITORS})
        written = io.writes.copy()
        self.assertFalse(apply_tcam(io, TCAM_PLAN)['changed'])
        self.assertEqual(io.writes, written)

    def test_active_or_unknown_state_is_untouched(self):
        for register, value in ((INIT,CLOCK_MASK), (TCAM_STATUS,1), (RST,0),
                                (TCAM[0],0), (TCAM[2],0x1234)):
            io = Registers(); io.values[register] = value
            with self.assertRaises(RuntimeError): apply_tcam(io, TCAM_PLAN)
            self.assertFalse(io.writes)

    def test_lock_timeout_restores_reset_state(self):
        io = Registers(); io.lock = False
        before = io.values.copy()
        with self.assertRaises(TimeoutError):
            apply_tcam(io, TCAM_PLAN, sleep=lambda _:None)
        self.assertEqual(io.values, before)

    def test_failed_readback_restores_state(self):
        io = Registers(); io.bad_write = TCAM[1]
        before = io.values.copy()
        with self.assertRaises(RuntimeError):
            apply_tcam(io, TCAM_PLAN, sleep=lambda _:None)
        self.assertEqual(io.values, before)

    def test_arbitrary_frequency_rejected(self):
        io = Registers(); plan = list(TCAM_PLAN); plan[1] = 255
        with self.assertRaises(RuntimeError): apply_tcam(io, plan)
        self.assertFalse(io.writes)


if __name__ == '__main__': unittest.main()
