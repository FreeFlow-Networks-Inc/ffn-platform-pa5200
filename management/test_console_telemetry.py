import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'octeon/debian'),str(ROOT/'management')]
# Core companion is required by the selected platform, also in deployment.
if os.environ.get('FFN_CORE_ROOT'):sys.path.insert(0,str(Path(os.environ['FFN_CORE_ROOT'])/'opt'))
import ffn_front_traffic as traffic
import mp_interfaces as mp
import chassis_storage


class TelemetryTests(unittest.TestCase):
    def test_only_24_data_ports_and_zero_counters(self):
        links=dict(stale=False,error=None,boot_id='test',ports=[dict(port=i,bcm_port=100+i,carrier=False,speed_mbps=None) for i in range(1,25)])
        reply=dict(ok=True,truncated=False,sample=['snmpEtherStatsRXNoErrors'],counters=[dict(port='101',dir='RX',counter='snmpEtherStatsRXNoErrors',value=123),dict(port='8',dir='RX',counter='snmpEtherStatsRXNoErrors',value=999999)])
        result=traffic.normalize(reply,links)
        self.assertEqual(len(result['ports']),24);self.assertEqual(result['ports'][0]['rx_packets_total'],123)
        self.assertEqual(sum(p['rx_packets_total'] for p in result['ports']),123)
        self.assertFalse(result['byte_rates_available'])
        links['stale']=True;self.assertIsNone(traffic.normalize(reply,links)['ports'][0]['link'])
        reply['truncated']=True
        with self.assertRaises(ValueError):traffic.normalize(reply,links)
    def test_storage_never_guesses_enumeration_as_bay(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'paths').mkdir();(p/'sys').mkdir()
            (p/'map').write_text('{}')
            result=chassis_storage.sample(p/'sys',p/'paths',p/'map')
            self.assertEqual(len(result['bays']),4)
            self.assertTrue(all(b['state']=='unmapped' for b in result['bays']))
            result=chassis_storage.sample(p/'sys',p/'paths',p/'absent')
            self.assertTrue(all(b['mapped'] and b['state']=='absent' for b in result['bays']))


class MPControllerTests(unittest.TestCase):
    def test_render_is_pci_scoped_and_disabled_stops_link(self):
        cfg=dict(mode='disabled',address='',gateway='',dns=[],mtu=1500,description='x')
        text=mp.render({'pci':'0000:0f:00.0'},cfg)
        self.assertIn('Path=pci-0000:0f:00.0',text);self.assertIn('ActivationPolicy=always-down',text)
        cfg['mode']='dhcp';self.assertIn('DHCP=ipv4',mp.render({'pci':'0000:0f:00.0'},cfg))
    def test_commit_digest_required_and_absent_preserves_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'running.xml';path.write_text('<config/>')
            with patch.object(mp,'RUNNING',path),patch.object(mp,'inventory',return_value=[]),patch.object(mp,'run',return_value='active') as run:
                state=mp.execute('status',{})
                with self.assertRaises(ValueError):mp.execute('apply',{'revision':state['revision'],'digest':'wrong'})
                import hashlib
                result=mp.execute('apply',dict(revision=state['revision'],digest=hashlib.sha256(path.read_bytes()).hexdigest()))
                self.assertEqual(result['applied'],[])
                self.assertEqual(run.call_count,1)
    def test_failed_reconfigure_restores_owned_file(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'running.xml';network=root/'network';network.mkdir()
            config.write_text('''<config><devices><entry name="localhost.localdomain"><deviceconfig><system><mp-interfaces>
              <entry name="MGT"><mode>disabled</mode></entry></mp-interfaces></system></deviceconfig></entry></devices></config>''')
            file=network/'05-ffn-mp-mgt.network';file.write_text('previous settings')
            port=dict(name='MGT',netdev='fixture0',pci='0000:0f:00.0')
            calls=[]
            def run(*args):
                calls.append(args)
                if args==('systemctl','is-active','systemd-networkd'):return 'active'
                if args==('networkctl','reconfigure','fixture0') and calls.count(args)==1:raise RuntimeError('fixture failure')
                return ''
            digest=hashlib.sha256(config.read_bytes()).hexdigest()
            with patch.object(mp,'RUNNING',config),patch.object(mp,'NETWORK',network),patch.object(mp,'BACKUPS',root/'backups'),patch.object(mp,'inventory',return_value=[port]),patch.object(mp,'run',side_effect=run):
                with self.assertRaises(RuntimeError):mp.execute('apply',dict(revision=int(digest[:12],16),digest=digest))
            self.assertEqual(file.read_text(),'previous settings')
            self.assertEqual(calls.count(('networkctl','reconfigure','fixture0')),2)


if __name__=='__main__':unittest.main()
