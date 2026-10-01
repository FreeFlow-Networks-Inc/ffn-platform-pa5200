import unittest
from ffn_fe100_guard import Withdrawal,group_path


class GuardTests(unittest.TestCase):
    def test_drain_order_and_strict_acknowledgement(self):
        for failure in (None,'ingress','sessions','resources'):
            with self.subTest(failure=failure):
                calls=[]
                def step(name):
                    def run(reason):
                        self.assertEqual(reason,'owner lost');calls.append(name)
                        return name!=failure
                    return run
                withdraw=Withdrawal(*(step(n) for n in ('ingress','sessions','resources')))
                if failure:
                    with self.assertRaisesRegex(RuntimeError,failure):withdraw('owner lost')
                    self.assertEqual(calls,['ingress','sessions','resources'][:['ingress','sessions','resources'].index(failure)+1])
                else:self.assertTrue(withdraw('owner lost'))

    def test_truthy_nonack_is_not_withdrawal(self):
        with self.assertRaises(RuntimeError):Withdrawal(lambda _:1,lambda _:True,lambda _:True)('lost')

    def test_group_path_requires_canonical_identity(self):
        for value in ('../other','anything','00000000000000000000000000000000'):
            with self.assertRaises(ValueError):group_path(value)


if __name__=='__main__':unittest.main()
