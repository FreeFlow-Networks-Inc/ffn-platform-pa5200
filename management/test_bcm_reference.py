import unittest
import ffn_bcm_reference as ref


class ReferenceTests(unittest.TestCase):
    def test_sdk_error_is_unavailable_not_zero_capacity(self):
        value=ref.probe('l3',lambda _:['FFN_REF rv=-16','FFN_REF_DONE'])
        self.assertEqual(value,dict(available=False,sdk_return=-16,values={}))

    def test_partial_or_echoed_reply_is_not_evidence(self):
        for lines in (['FFN_REF rv=0 1 2','FFN_REF_DONE'],
                      ['printf("FFN_REF rv=0 1 2 3");','FFN_REF_DONE'],
                      ['FFN_REF rv=0 1 2 3']):
            with self.assertRaises(RuntimeError):ref.probe('device',lambda _:lines)

    def test_64_bit_counters_are_exact_and_failed_counter_has_no_value(self):
        lines=['FFN_STAT 32 %d %d fffffffffffffffe'%(i,-16 if i==5 else 0) for i in range(6)]+['FFN_REF_DONE']
        result=ref.counters([32],lambda _:lines)['32']
        self.assertEqual(result[ref.COUNTERS[0]]['value'],'18446744073709551614')
        self.assertIsNone(result[ref.COUNTERS[5]]['value'])
        self.assertFalse(result[ref.COUNTERS[5]]['available'])

    def test_counter_reply_must_match_every_requested_key_once(self):
        baseline=['FFN_STAT 32 %d 0 0000000000000000'%i for i in range(6)]
        for lines in (baseline[:-1],baseline+[baseline[0]],
                      [baseline[0].replace('32','33')]+baseline[1:]):
            with self.assertRaises(RuntimeError):ref.counters([32],lambda _:lines+['FFN_REF_DONE'])

    def test_untrusted_ports_never_reach_sdk(self):
        def run(_):self.fail('Invalid request reached SDK')
        for ports in ([],[True],[32,32],['32;exit;'],[-1],[256],list(range(9))):
            with self.assertRaises(ValueError):ref.counters(ports,run)


if __name__=='__main__':unittest.main()
