import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_platform_policy_bindings as binding


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.run=self.root/'run';self.proc=self.root/'proc'
        self.run.mkdir();(self.proc/'sys/kernel/random').mkdir(parents=True);(self.proc/'42').mkdir()
        self.token='12345678-1234-4234-8234-123456789abc'
        (self.proc/'sys/kernel/random/boot_id').write_text(self.token)
        self.fields=['S']+['0']*18+['900']
        (self.proc/'42/stat').write_text('42 (test owner) '+' '.join(self.fields))
        self.row=dict(group='ae7',token=self.token,boot_id=self.token,pid=42,process_start='900',
                      updated_monotonic=100,configuration_revision='a'*64,gates_verified=True,
                      distributing=[1,2],network_ready=True,attachment_ready=True,network={'enabled':True},
                      subinterfaces=[{'name':'ae7.123','tag':123,'applied':True}])
        self.links={'ae7':{'ifindex':10,'ifalias':'ffn-aggregate:'+self.token},
                    'ae7.123':{'link_index':10,'ifalias':'ffn-aggregate:'+self.token+':ae7.123',
                               'linkinfo':{'info_kind':'vlan','info_data':{'id':123}}}}
    def discover(self):
        path=self.run/'ffn-aggregate-ae7-status.json';path.write_text(json.dumps(self.row));path.chmod(0o600)
        # Windows fixtures have no Unix ownership semantics.
        with patch.object(binding,'trusted',return_value=True):
            return binding.discover(self.links,self.run,self.proc,102)
    def test_live_owner_and_child(self):
        self.assertEqual(self.discover(),{'ae7':'ae7','ae7.123':'ae7.123'})
    def test_stale_dead_or_replaced_owner_withdrawn(self):
        for key,value in [('boot_id','old'),('process_start','901'),('updated_monotonic',95),
                          ('updated_monotonic',103),('gates_verified',False),('network_ready',False),
                          ('distributing',[]),('control_only',True),('network_update_pending',True)]:
            old=copy.deepcopy(self.row);self.row[key]=value
            self.assertEqual(self.discover(),{},key);self.row=old
    def test_child_requires_alias_tag_parent_and_apply(self):
        baseline=copy.deepcopy(self.links)
        for change in ({'ifalias':'foreign'},{'link_index':11},{'master':'bridge'},
                       {'linkinfo':{'info_kind':'vlan','info_data':{'id':124}}}):
            self.links['ae7.123'].update(change)
            self.assertNotIn('ae7.123',self.discover());self.links=copy.deepcopy(baseline)
        self.row['subinterfaces'][0]['applied']=False
        self.assertNotIn('ae7.123',self.discover())
    def test_new_generation_recovered_without_static_mapping(self):
        self.assertIn('ae7.123',self.discover())
        self.row['token']='99999999-1234-4234-8234-123456789abc'
        self.assertEqual(self.discover(),{})
        for link in self.links.values():link['ifalias']=link['ifalias'].replace(self.token,self.row['token'])
        self.assertIn('ae7.123',self.discover())


if __name__=='__main__':unittest.main()
