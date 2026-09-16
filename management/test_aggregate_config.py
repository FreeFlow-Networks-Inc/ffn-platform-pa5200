import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from aggregate_config import plan,readiness
from aggregate_backend import execute

XML=b'''<config><devices><entry name="localhost.localdomain"><network><interface>
<ethernet><entry name="ethernet1/23"><aggregate-group>ae1</aggregate-group></entry>
<entry name="ethernet1/24"><aggregate-group>ae1</aggregate-group></entry></ethernet>
<aggregate-ethernet><entry name="ae1"><lldp><enable>yes</enable></lldp><layer3>
<dhcp-client><enable>yes</enable><create-default-route>no</create-default-route></dhcp-client>
<bond><mode>802.3ad</mode><miimon>100</miimon></bond></layer3></entry></aggregate-ethernet>
</interface></network></entry></devices></config>'''
FACE={'ports':[{'port':p,'available':True,'enabled':True,'link':True,'speed_mbps':100000} for p in (23,24)]}


class AggregateTests(unittest.TestCase):
    def test_current_webui_xml_compiles_to_physical_members(self):
        result=plan(XML);group=result['aggregates'][0]
        self.assertEqual(group['errors'],[]);self.assertEqual(result['orphan_members'],[])
        self.assertEqual([p['bcm_port'] for p in group['members']],[34,35])
        self.assertTrue(group['network']['dhcp']);self.assertFalse(group['network']['dhcp_default_route'])
        self.assertEqual(group['lacp']['activity'],'active')

    def test_validation_reports_bad_members_references_and_settings(self):
        for raw in (XML.replace(b'ethernet1/24',b'ethernet1/23'),XML.replace(b'ae1',b'ae99'),
                    XML.replace(b'802.3ad',b'balance-rr'),XML.replace(b'<layer3>',b'<layer3><ip><entry name="192.0.2.1/24"/></ip>'),
                    XML.replace(b'<layer3>',b'<layer3><mtu>bad</mtu>')):
            self.assertTrue(plan(raw)['aggregates'][0]['errors'])
        orphan=plan(XML.replace(b'<aggregate-group>ae1',b'<aggregate-group>ae2'))
        self.assertEqual(len(orphan['orphan_members']),2)
        for raw in (b'<!DOCTYPE config><config/>',XML.decode().encode('utf-16')):
            with self.assertRaises(ValueError):plan(raw)

    def test_link_up_does_not_claim_lacp_or_forwarding(self):
        group=plan(XML)['aggregates'][0]
        result=readiness(group,FACE,{'backend':{'ports':[1]}},{'available':True,'ports':[]})
        self.assertEqual(result['state'],'blocked');self.assertFalse(result['applied'])
        self.assertTrue(all(p['link'] for p in result['members']))
        self.assertFalse(any(p['attached'] for p in result['members']))
        self.assertEqual({b['code'] for b in result['blockers']},{'bcm-membership','lacp-negotiation','dataplane-attachment','dhcp-client','lldp'})

    def test_peer_mismatch_and_failed_observations(self):
        group=plan(XML)['aggregates'][0]
        peers={'available':True,'ports':[{'port':p,'expired':False,'actor':{'system':str(p),'key':1}} for p in (23,24)]}
        result=readiness(group,FACE,{},peers)
        self.assertIn('partner-mismatch',[b['code'] for b in result['blockers']])
        self.assertEqual(sum(b['code']=='member-unavailable' for b in readiness(group,{}, {},{})['blockers']),2)

    def test_daemon_reads_both_configs_and_never_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            for source in ('candidate','running'):(path/(source+'-config.xml')).write_bytes(XML)
            backend=AsyncMock()
            async def query(resource,action):
                self.assertEqual(action,'status')
                return FACE if resource=='faceplate' else {}
            backend.run.side_effect=query
            result=asyncio.run(execute('status',{},backend,path))
            self.assertTrue(result['aggregates'][0]['committed'])
            self.assertEqual(result['candidate_revision'],result['running_revision'])
            (path/'candidate-config.xml').write_bytes(XML.replace(b'<create-default-route>no',b'<create-default-route>yes'))
            result=asyncio.run(execute('status',{},backend,path))
            self.assertFalse(result['aggregates'][0]['committed'])
            self.assertEqual((path/'running-config.xml').read_bytes(),XML)
            with self.assertRaises(ValueError):asyncio.run(execute('apply',{},backend,path))

    def test_cli_uses_shared_authenticated_endpoint(self):
        from cli_extension import handle
        from contextlib import redirect_stdout
        import io
        calls=[]
        with redirect_stdout(io.StringIO()) as output:
            self.assertTrue(handle('show platform aggregates',lambda path,token:(calls.append((path,token)) or {'aggregates':[]}),'fixture'))
        self.assertEqual(calls,[('/api/interfaces/aggregate-status','fixture')])
        self.assertEqual(json.loads(output.getvalue()),{'aggregates':[]})


if __name__=='__main__':unittest.main()
