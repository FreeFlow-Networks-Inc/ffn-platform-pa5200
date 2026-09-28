import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_platform_policy_bindings as binding


class BindingTests(unittest.TestCase):
    def candidate(self,extra=''):
        return '''<config><devices><entry><network><interface><ethernet>
        <entry name="ethernet1/21"><aggregate-group>ae1</aggregate-group></entry>
        <entry name="ethernet1/22"><aggregate-group>ae1</aggregate-group></entry>
        </ethernet><aggregate-ethernet><entry name="ae1"><layer3>
        <ip><entry name="LAN gateway"/></ip>'''+extra+'''</layer3></entry></aggregate-ethernet>
        </interface></network><vsys><entry name="vsys1"><address><entry name="LAN gateway">
        <ip-netmask>192.0.2.1/24</ip-netmask></entry></address></entry></vsys></entry></devices></config>'''

    def test_candidate_preview_resolves_objects_without_commissioning(self):
        live={'ethernet1/1':dict(device='p1',index=4,alias='wan')};addresses={'p1':dict(ifname='p1')}
        current,rows,pending=binding.preview(self.candidate(),live,addresses)
        self.assertEqual(pending,['ae1']);self.assertNotIn('ae1',live)
        self.assertEqual(addresses,{'p1':dict(ifname='p1')})
        self.assertEqual(rows[current['ae1']['device']]['addr_info'][0]['local'],'192.0.2.1')
        self.assertEqual(current['ae1']['alias'],'candidate-preview')
        self.assertEqual(binding.discover({},self.run,self.proc,102),{})

    def test_candidate_units_and_invalid_references(self):
        unit='<units><entry name="ae1.69"><tag>69</tag><ip><entry name="198.51.100.1/24"/></ip></entry></units>'
        current,rows,pending=binding.preview(self.candidate(unit),{},{});self.assertEqual(pending,['ae1','ae1.69'])
        for xml in [self.candidate().replace('ethernet1/22','ethernet1/21'),
                    self.candidate().replace('name="LAN gateway"/>','name="missing"/>'),
                    self.candidate(unit.replace('<tag>69','<tag>4095'))]:
            with self.assertRaises(ValueError):binding.preview(xml,{},{})

    def test_candidate_preview_never_invents_dhcp_address(self):
        xml=self.candidate().replace('<ip><entry name="LAN gateway"/></ip>','<dhcp-client><enable>yes</enable></dhcp-client>')
        current,rows,pending=binding.preview(xml,{},{})
        self.assertEqual(pending,['ae1']);self.assertEqual(rows[current['ae1']['device']]['addr_info'],[])

    def test_disconnected_physical_interface_and_object_validate_without_owner(self):
        xml=self.candidate().replace('</ethernet>','<entry name="ethernet1/5"><link-state>down</link-state><layer3><ip><entry name="LAN gateway"/></ip></layer3></entry></ethernet>')
        current,rows,pending=binding.preview(xml,{},{})
        self.assertIn('ethernet1/5',pending)
        self.assertEqual(rows[current['ethernet1/5']['device']]['addr_info'][0]['local'],'192.0.2.1')
        self.assertEqual(binding.discover({},self.run,self.proc,102),{})

    def test_removed_or_non_routed_interface_cannot_use_stale_binding(self):
        live={'ethernet1/5':dict(device='p5',index=5,alias='old')}
        self.assertNotIn('ethernet1/5',binding.preview(self.candidate(),live,{})[0])

    def test_candidate_admin_down_aggregate_is_still_configurable(self):
        xml=self.candidate().replace('<entry name="ae1">','<entry name="ae1"><link-state>down</link-state>')
        self.assertIn('ae1',binding.preview(xml,{}, {})[0])

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
                          ('control_only',True),('network_update_pending',True)]:
            old=copy.deepcopy(self.row);self.row[key]=value
            self.assertEqual(self.discover(),{},key);self.row=old

    def test_configured_owner_without_carrier_or_dhcp_lease_remains_bound(self):
        self.row.update(distributing=[],network_ready=False,configuration_ready=True)
        self.assertEqual(self.discover(),{'ae7':'ae7','ae7.123':'ae7.123'})
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

    def test_only_acknowledged_parent_gets_a_conditional_guard(self):
        with patch.object(binding,'discover',return_value={'ae7.123':'ae7.123','ethernet1/1':'p1'}):
            guards=binding.security_guards(self.links)
        self.assertEqual(set(guards),{'ffn_aggregate_ae7'})
        self.assertIn('"ae7.*"',guards['ffn_aggregate_ae7'])
        self.assertEqual(guards['ffn_aggregate_ae7'].count('meta mark & 0x80000000 == 0 counter drop'),2)
        with patch.object(binding,'discover',return_value={}):
            self.assertEqual(binding.security_guards(self.links),{})

    def test_wan_binding_requires_current_process_heartbeat_and_tap(self):
        row=dict(owner='wan1',boot_id=self.token,pid=42,process_start='900',ports=[1],
                 updated_monotonic=100,interfaces={'p1':4})
        link=dict(ifindex=4,flags=['UP'],linkinfo=dict(info_kind='tun',info_data={'type':'tap'}))
        path=self.run/'ffn-fabric.json'
        def read():
            path.write_text(json.dumps(row))
            with patch.object(binding,'trusted',return_value=True):
                return binding.discover({'p1':link},self.run,self.proc,102)
        self.assertEqual(read(),{'ethernet1/1':'p1'})
        for key,value in [('boot_id','old'),('process_start','replaced'),('updated_monotonic',96),('ports',[2])]:
            old=row[key];row[key]=value;self.assertEqual(read(),{});row[key]=old
        for key,value in [('ifindex',5),('flags',[]),('master','bridge')]:
            saved=copy.deepcopy(link);link[key]=value;self.assertEqual(read(),{});link=saved


if __name__=='__main__':unittest.main()
