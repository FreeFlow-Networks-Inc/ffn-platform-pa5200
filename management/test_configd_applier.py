import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
from configd_applier import PlatformApplier
import configd_applier

class Status:
    def __init__(self): self.applied=[];self.errors=[]
    def ok(self,*args): self.applied.append(args)
    def fail(self,*args): self.errors.append(args)

class ApplyTests(unittest.TestCase):
    def test_missing_address_object_fails_before_any_hardware_calls(self):
        xml='<config><devices><entry name="localhost.localdomain"><network><interface><ethernet><entry name="ethernet1/1"><layer3><ip><entry name="missing"/></ip></layer3></entry></ethernet></interface></network></entry></devices></config>'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_text(xml)
            with patch('configd_applier.rpc') as rpc, self.assertRaisesRegex(ValueError,'missing'):
                PlatformApplier(path).reconcile(Status())
            rpc.assert_not_called()
    def test_explicit_lldp_off_is_supported_but_enabled_and_unknown_options_are_not(self):
        from xml.etree import ElementTree as ET
        for body in ('','<lldp/>','<lldp><enable>no</enable></lldp>'):
            configd_applier.validate_physical_options(ET.fromstring('<entry><layer3/>'+body+'</entry>'))
        for body in ('<lldp><enable>yes</enable></lldp>','<lldp><profile>custom</profile></lldp>',
                     '<lldp><enable>invalid</enable></lldp>','<lldp><enable>no</enable><enable>yes</enable></lldp>',
                     '<lldp/><lldp/>','<layer3><unexpected/></layer3>','<layer2/>'):
            with self.assertRaises(ValueError):configd_applier.validate_physical_options(ET.fromstring('<entry>'+body+'</entry>'))

    def test_none_and_omitted_front_ports_disable_data_and_physical_link(self):
        xml='''<config><devices><entry name="localhost.localdomain"><network><interface><ethernet>
        <entry name="ethernet1/1"><layer3><ip><entry name="192.0.2.1/24"/></ip></layer3><lldp><enable>no</enable></lldp></entry>
        <entry name="ethernet1/2"><comment>Unconfigured</comment><link-state>up</link-state></entry>
        <entry name="ethernet1/24"/>
        </ethernet></interface></network></entry></devices></config>'''
        face={'revision':1,'ports':[{'port':p,'enabled':True,'available':True} for p in (1,2,3,24)]}
        net={'config':{'revision':1,'ports':{
            'p1':{'mode':'l3','addresses':['192.0.2.1/24']},
            'p2':{'mode':'l3','addresses':['198.51.100.1/24']},
            'p3':{'mode':'l2','vlans':[100],'pvid':100}}},'backend':{'ports':[1]}}
        calls=[]
        def rpc(resource,action='status',payload=None):
            calls.append((resource,action,payload))
            if action=='status':return copy.deepcopy(face if resource=='faceplate' else net)
            if resource=='faceplate':
                self.assertFalse(payload['enabled'])
                next(row for row in face['ports'] if row['port']==payload['port'])['enabled']=False
                face['revision']+=1
                return {'data':copy.deepcopy(face)}
            self.assertEqual(resource,'network')
            net['config']['ports'].update(payload['ports']);net['config']['revision']+=1
            return {}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_text(xml)
            status=Status()
            with patch('configd_applier.rpc',side_effect=rpc):PlatformApplier(path).reconcile(status)
        self.assertEqual(status.errors,[])
        self.assertEqual(net['config']['ports']['p1']['addresses'],['192.0.2.1/24'])
        self.assertEqual(net['config']['ports']['p2'],{'mode':'disabled'})
        self.assertEqual(net['config']['ports']['p3'],{'mode':'disabled'})
        self.assertEqual({p['port'] for r,a,p in calls if (r,a)==('faceplate','apply')},{2,3,24})
        self.assertTrue(face['ports'][0]['enabled'])

    def test_routes_are_read_from_xml_and_deletions_do_not_use_legacy_sql(self):
        from xml.etree import ElementTree as ET
        dev=ET.fromstring('''<entry><network><virtual-router><ffn-candidate-managed>yes</ffn-candidate-managed>
        <entry name="default"><routing-table><ip><static-route><entry name="default"><destination>0.0.0.0/0</destination>
        <nexthop><ip-address>192.0.2.254</ip-address></nexthop><interface>ethernet1/1</interface><metric>100</metric>
        </entry></static-route></ip></routing-table></entry></virtual-router></network></entry>''')
        config={'ports':{'p1':{'mode':'l3','addresses':['192.0.2.1/24']}}}
        self.assertEqual(configd_applier.committed_routes(dev,config),[
            {'dst':'0.0.0.0/0','via':'192.0.2.254','dev':'p1','metric':100}])
        dev.find('.//static-route').clear()
        with patch.object(configd_applier.sqlite3,'connect') as db:
            self.assertEqual(configd_applier.committed_routes(dev,config),[])
            db.assert_not_called()

    def test_configd_uses_controld_without_subprocess_fallback(self):
        client=Mock();client.plane_request.return_value={'ok':True,'result':{'revision':9}}
        with patch.dict('sys.modules',{'ffn_controld_client':SimpleNamespace(ControldClient=Mock(return_value=client))}), \
                patch.object(configd_applier.subprocess,'run') as direct:
            self.assertEqual(configd_applier.rpc('network'),{'revision':9})
            request=client.plane_request.call_args.args[0]
            self.assertEqual(request['resource'],'network')
            self.assertEqual(request['action'],'status')
            client.plane_request.side_effect=RuntimeError('controld unavailable')
            with self.assertRaises(RuntimeError):configd_applier.rpc('network','apply',{'revision':9})
            direct.assert_not_called()

    def test_interface_config_reaches_mp_and_unsupported_is_error(self):
        xml='''<config><devices><entry name="localhost.localdomain"><network><interface><ethernet>
        <entry name="ethernet1/1"><layer3/></entry>
        <entry name="ethernet1/5"><link-state>down</link-state><layer3/></entry>
        <entry name="ethernet1/21"><aggregate-group>ae1</aggregate-group></entry>
        </ethernet><aggregate-ethernet><entry name="ae1"><layer3/></entry></aggregate-ethernet>
        </interface></network></entry></devices></config>'''
        face={'revision':1,'ports':[{'port':p,'enabled':True,'available':True} for p in (1,5,21)]}
        net={'config':{'revision':1,'ports':{'p1':{'mode':'l2'},'p5':{'mode':'l3','addresses':['192.0.2.1/24']}}},'backend':{'ports':[1,5]}}
        calls=[]
        def rpc(resource,action='status',payload=None):
            calls.append((resource,action,payload))
            if action=='status': return copy.deepcopy(face if resource=='faceplate' else net)
            if resource=='faceplate':
                next(p for p in face['ports'] if p['port']==payload['port'])['enabled']=payload['enabled']
                face['revision']+=1;return {'data':copy.deepcopy(face)}
            net['config']['ports'].update(payload['ports']);net['config']['revision']+=1
            return {}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_text(xml)
            status=Status()
            with patch('configd_applier.rpc',side_effect=rpc): PlatformApplier(path).reconcile(status)
        self.assertEqual(net['config']['ports']['p1'],{'mode':'disabled'})
        self.assertEqual(net['config']['ports']['p5'],{'mode':'disabled'})
        self.assertFalse(face['ports'][1]['enabled'])
        self.assertEqual({e[0] for e in status.errors},{'ethernet1/21','ae1'})
        self.assertTrue(all(c[2]['port']==5 for c in calls if c[:2]==('faceplate','apply')))

if __name__=='__main__': unittest.main()
