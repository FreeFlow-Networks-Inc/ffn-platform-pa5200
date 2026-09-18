import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_aggregate_dhcp as dhcp


class DHCPTests(unittest.TestCase):
    def test_replaced_client_cannot_reinstall_old_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);intent={'boot_id':'fixture','token':'owner','control_only':False,'network_generation':'new','network':{'dhcp':True}}
            (root/'ffn-aggregate-ae1-intent.json').write_text(json.dumps(intent))
            with patch.object(dhcp,'RUNDIR',root),patch.object(dhcp,'boot',return_value='fixture'),patch.object(dhcp,'ip') as ip:
                with self.assertRaisesRegex(ValueError,'Stale aggregate DHCP client'):
                    dhcp.execute('bound',{'interface':'ae1','FFN_AGGREGATE_NETWORK_REVISION':'old'})
                ip.assert_not_called()
    def test_invalid_offer_cannot_supply_arbitrary_commands_or_offlink_gateway(self):
        for env in ({'ip':'192.0.2.4;bad','subnet':'255.255.255.0'},
                    {'ip':'192.0.2.4','subnet':'255.255.255.0','router':'198.51.100.1'},
                    {'ip':'192.0.2.4','subnet':'255.0.255.0'}):
            with self.assertRaises(ValueError):dhcp.lease_values(env)
        with self.assertRaises(KeyError):dhcp.lease_values({'ip':'192.0.2.4'})

    def test_lease_never_replaces_an_unrelated_route_and_failures_are_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[];failure=[False]
            intent={'boot_id':'fixture','token':'owner','control_only':False,'network':{'dhcp':True,'dhcp_default_route':False,'dhcp_route_metric':50,'management':{}}}
            path=root/'ffn-aggregate-ae1-intent.json';path.write_text(json.dumps(intent))
            def ip(*args):
                calls.append(args)
                if args==('-j','link','show','dev','ae1'):return json.dumps([{'ifalias':'ffn-aggregate:owner'}])
                if args[:2]==('route','add') and failure[0]:raise RuntimeError('route conflict')
                return ''
            env={'interface':'ae1','ip':'192.0.2.20','subnet':'255.255.255.0','router':'192.0.2.1'}
            with patch.object(dhcp,'RUNDIR',root),patch.object(dhcp,'boot',return_value='fixture'),patch.object(dhcp,'ip',side_effect=ip),patch.object(dhcp,'apply'):
                dhcp.execute('bound',env)
                self.assertFalse(any(c[0]=='route' for c in calls));self.assertIn(('address','add','192.0.2.20/24','dev','ae1'),calls)
                intent['network']['dhcp_default_route']=True;path.write_text(json.dumps(intent));failure[0]=True
                with self.assertRaises(RuntimeError):dhcp.execute('renew',env)
                value=json.loads((root/'ffn-aggregate-ae1-lease.json').read_text());self.assertTrue(value['error']);self.assertIsNone(value['router'])
                self.assertFalse(any('flush' in c or 'replace' in c for c in calls))
                self.assertIn(('route','add','default','via','192.0.2.1','dev','ae1','proto','186','metric','50'),calls)


if __name__=='__main__':unittest.main()
