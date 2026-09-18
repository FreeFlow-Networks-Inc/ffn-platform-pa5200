import builtins
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import ffn_aggregate_runtime as runtime
from ffn_interface_management import profile

class NetworkUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.network=dict(enabled=True,addresses=['192.0.2.2/24'],dhcp=False,dhcp_default_route=False,dhcp_route_metric=10,mtu=1500,management=profile(None,''))
        self.intent=dict(group='ae1',token=str(uuid.uuid4()),boot_id='fixture',system='02:00:00:00:10:01',members=[23,24],lacp=dict(activity='active',rate='fast',min_links=1,system_priority=32768),network=self.network,lldp=True,control_only=False,offload=False)
        self.old=dict(self.network,addresses=['192.0.2.1/24']);self.calls=[]
        self.write_intent()
    def tearDown(self):self.tmp.cleanup()
    def write_intent(self):
        (self.root/'ffn-aggregate-ae1-intent.json').write_text(json.dumps(dict(self.intent,network_generation=runtime.network_revision(self.intent))))
    def ip(self,*args):
        self.calls.append(args)
        if args[:3]==('-j','link','show'):return json.dumps([{'ifalias':'ffn-aggregate:'+self.intent['token']}])
        if args[:3]==('-j','address','show'):return json.dumps([{'addr_info':[{'local':'192.0.2.1','prefixlen':24},{'local':'198.51.100.1','prefixlen':24}]}])
        return ''
    def apply(self):
        with patch.object(runtime,'Path',side_effect=lambda p:self.root/Path(p).name),patch.object(runtime,'open',side_effect=lambda p,*a:builtins.open(self.root/Path(p).name,*a),create=True),patch.object(runtime,'boot',return_value='fixture'),patch.object(runtime,'ip',side_effect=self.ip),patch('ffn_interface_management.apply'),patch.object(runtime.vlan,'reconcile',return_value=[]):
            return runtime.apply_network_update(dict(intent=self.intent,previous=self.old))
    def test_update_changes_only_owned_addresses_without_recreating_link(self):
        ack=self.apply();self.assertTrue(ack['ok']);self.assertEqual(ack['revision'],runtime.network_revision(self.intent))
        self.assertIn(('address','del','192.0.2.1/24','dev','ae1'),self.calls)
        self.assertIn(('address','add','192.0.2.2/24','dev','ae1'),self.calls)
        self.assertFalse(any('198.51.100.1/24' in c or 'flush' in c or c[:2]==('link','delete') or c[0]=='tuntap' for c in self.calls))
    def test_link_only_removes_parent_address_without_enabling_tap(self):
        self.intent['network']=dict(self.network,enabled=False,addresses=[]);self.write_intent();self.apply()
        self.assertIn(('link','set','ae1','down'),self.calls)
        self.assertNotIn(('link','set','ae1','up'),self.calls)
    def test_superseded_update_is_rejected_before_mutation(self):
        saved=json.loads((self.root/'ffn-aggregate-ae1-intent.json').read_text());saved['network_generation']='newer'
        (self.root/'ffn-aggregate-ae1-intent.json').write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError,'superseded'):self.apply()
        self.assertEqual(self.calls,[])
    def test_failure_propagates_without_claiming_applied(self):
        self.ip=lambda *args:(_ for _ in ()).throw(RuntimeError('injected'))
        with self.assertRaisesRegex(RuntimeError,'injected'):self.apply()

    def test_retry_removes_owned_partial_update_and_preserves_unrelated_address(self):
        pending=self.root/'ffn-aggregate-ae1-network-pending.json'
        pending.write_text(json.dumps(dict(token=self.intent['token'],addresses=['192.0.2.9/24'])))
        original=self.ip
        def ip(*args):
            result=original(*args)
            if args[:3]==('-j','address','show'):
                rows=json.loads(result);rows[0]['addr_info'].append(dict(local='192.0.2.9',prefixlen=24));return json.dumps(rows)
            return result
        self.ip=ip;self.apply()
        self.assertIn(('address','del','192.0.2.9/24','dev','ae1'),self.calls)
        self.assertFalse(pending.exists())
        self.assertFalse(any('198.51.100.1/24' in c for c in self.calls))

if __name__=='__main__':unittest.main()
