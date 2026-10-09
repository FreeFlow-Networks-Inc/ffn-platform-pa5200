import unittest
import ffn_bcm_trunk as trunk


class Chip:
    def __init__(self,reply):self.reply=reply;self.calls=[]
    def run(self,script):self.calls.append(script);return self.reply


class TrunkTests(unittest.TestCase):
    def test_foreign_existing_trunk_is_not_adopted(self):
        chip=Chip('FFN_TRUNK_OP -8 1 0 0\nFFN_TRUNK_DONE\n')
        with self.assertRaises(RuntimeError):trunk.create(chip,dict(tid=5,ports=[32,33]))
        self.assertIn('if(mode==1 && checked && rv==-7)',chip.calls[0])

    def test_destroy_requires_expected_members_before_hardware(self):
        chip=Chip('')
        with self.assertRaises(ValueError):trunk.destroy(chip,dict(tid=5))
        self.assertFalse(chip.calls)

    def test_destroy_must_read_back_absent(self):
        chip=Chip('FFN_TRUNK_OP 0 1 0 0\nFFN_TRUNK 5 -7 0 0\nFFN_TRUNK_DONE\n')
        self.assertTrue(trunk.destroy(chip,dict(tid=5,ports=[]))['destroyed'])
        chip.reply=chip.reply.replace('5 -7 0 0','5 0 0 9')
        with self.assertRaises(RuntimeError):trunk.destroy(chip,dict(tid=5,ports=[]))

    def test_create_does_not_accept_wrong_members_or_psc(self):
        for row in ('5 0 1 9\nFFN_TRUNK_MEMBER 0 33 0','5 0 1 7\nFFN_TRUNK_MEMBER 0 32 0',
                    '5 0 1 9\nFFN_TRUNK_MEMBER 0 32 1'):
            chip=Chip('FFN_TRUNK_OP 0 0 1 0\nFFN_TRUNK '+row+'\nFFN_TRUNK_DONE\n')
            with self.assertRaises(RuntimeError):trunk.create(chip,dict(tid=5,ports=[32]))

    def test_partial_echoed_duplicate_and_failed_reads_rejected(self):
        for reply in ('FFN_TRUNK_DONE\n','FFN_TRUNK_OP 0 0 1 0\n',
                      'FFN_TRUNK_OP 0 0 1 0\nFFN_TRUNK 5 0 1 9\nFFN_TRUNK_MEMBER -4 32 0\nFFN_TRUNK_DONE\n',
                      'FFN_TRUNK_OP 0 0 1 0\nFFN_TRUNK 5 0 2 9\nFFN_TRUNK_MEMBER 0 32 0\nFFN_TRUNK_MEMBER 0 32 0\nFFN_TRUNK_DONE\n'):
            with self.assertRaises(RuntimeError):trunk.read(Chip(reply),dict(tid=5))


if __name__=='__main__':unittest.main()
