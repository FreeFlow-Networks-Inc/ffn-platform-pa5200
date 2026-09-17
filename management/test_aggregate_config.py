import asyncio
import copy
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
    def test_vlan_units_do_not_block_parent_lacp(self):
        raw=XML.replace(b'</bond>',b'</bond><units><entry name="ae1.69"><tag>69</tag><ip><entry name="192.0.2.1/24"/></ip></entry></units>')
        group=plan(raw)['aggregates'][0]
        self.assertEqual(group['errors'],[])
        self.assertEqual(group['subinterfaces'][0]['name'],'ae1.69')
        self.assertFalse(group['subinterfaces'][0]['applied'])
        self.assertEqual(group['subinterfaces'][0]['state'],'unsupported')

    def test_disabled_member_lldp_from_interface_editor_is_accepted(self):
        raw=XML.replace(b'<aggregate-group>ae1</aggregate-group>',b'<aggregate-group>ae1</aggregate-group><lldp><enable>no</enable></lldp>')
        self.assertEqual(plan(raw)['aggregates'][0]['errors'],[])
        self.assertTrue(plan(raw.replace(b'<enable>no',b'<enable>yes'))['aggregates'][0]['errors'])

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

    def test_down_member_speed_is_not_negotiated_speed(self):
        face=copy.deepcopy(FACE);face['ports'][0].update(enabled=False,link=False,speed_mbps=20000)
        face['ports'][1].update(link=False)
        result=readiness(plan(XML)['aggregates'][0],face,{}, {})
        self.assertIsNone(result['members'][0]['speed_mbps'])
        self.assertEqual(result['members'][0]['reported_speed_mbps'],20000)
        self.assertEqual([b['code'] for b in result['blockers']][:2],['member-admin-down','member-link-down'])
        self.assertNotIn('member-speed',[b['code'] for b in result['blockers']])

    def test_vpc_identity_checks_use_actor_not_frame_source(self):
        group=plan(XML)['aggregates'][0]
        peers={'available':True,'ports':[dict(port=p,expired=False,source='02:00:00:00:00:%02x'%p,
            actor=dict(system='02:00:00:00:00:01',system_priority=32768,key=100,port=p)) for p in (23,24)]}
        result=readiness(group,FACE,{},peers)
        self.assertEqual(result['partner_consistency']['state'],'consistent')
        self.assertFalse(result['applied']);self.assertFalse(result['partner_consistency']['negotiated'])
        for change,code in (({'system_priority':1},'partner-mismatch'),({'system':'02:00:00:00:00:02'},'partner-mismatch'),
                            ({'key':101},'partner-mismatch'),({'port':23},'partner-port-duplicate')):
            altered=copy.deepcopy(peers);altered['ports'][1]['actor'].update(change)
            result=readiness(group,FACE,{},altered)
            self.assertEqual(result['partner_consistency']['state'],'mismatch')
            self.assertIn(code,[b['code'] for b in result['blockers']])
        peers['ports'][1]['expired']=True
        result=readiness(group,FACE,{},peers)
        self.assertEqual(result['partner_consistency']['state'],'incomplete')
        self.assertEqual(result['partner_consistency']['missing_members'],['ethernet1/24'])

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
