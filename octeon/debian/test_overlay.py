import copy
import unittest
from unittest.mock import patch
import ffn_overlay as ov


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.network = {'ports': {'p5': {'mode':'l3', 'addresses':['198.18.1.1/24']}}}
        self.link = {'kind':'vxlan','underlay':'p5','local':'198.18.1.1','remote':'198.18.1.2','vni':5220}

    def test_reject_invalid_underlay_and_endpoints(self):
        for update in ({'mtu':1500}, {'vni':True}, {'remote':'224.0.0.1'},
                       {'local':'198.18.1.3'}, {'key':5}, {'replay_window':64}):
            c = {'revision':0,'links':{'ovtest':{**self.link,**update}}}
            with self.subTest(update=update), self.assertRaises(ValueError):
                ov.validate(c,self.network)

    def test_macsec_keys_never_accepted(self):
        c={'revision':0,'links':{'ovsec':{'kind':'macsec','underlay':'p5','key':'secret'}}}
        with self.assertRaises(ValueError): ov.validate(c,self.network)
        c['links']['ovsec'].pop('key')
        ov.validate(c,self.network)
        self.assertEqual(ov.create_args('ovsec',c['links']['ovsec'])[-2:],['window','64'])

    def test_save_failure_rolls_back_previous_link(self):
        old={'revision':0,'links':{'ovtest':self.link}}
        new=copy.deepcopy(old)
        new['links']['ovtest']['vni']=5221
        with patch.object(ov,'check_ownership'), patch.object(ov,'remove') as remove, \
             patch.object(ov,'create') as create, patch.object(ov,'save',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError,'rollback errors: \\[\\]'): ov.apply(old,new)
        self.assertEqual(create.call_args_list[-1].args,('ovtest',self.link))
        self.assertEqual(remove.call_count,2)

    def test_unowned_interface_rejected(self):
        with patch.object(ov,'ip',return_value='[{"ifname":"ovtest"}]'):
            with self.assertRaisesRegex(ValueError,'unowned'): ov.check_ownership(['ovtest'])


if __name__ == '__main__': unittest.main()
