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
    def test_dhcp_servers_are_applied_as_one_intent_and_read_back(self):
        import sys
        from types import ModuleType
        intent={'ae1.69':dict(interface='ae1.69',address='10.1.0.2/22',pools=[['10.1.0.100','10.1.0.199']],reserved={},lease=86400,probe=False,options={})}
        fake=ModuleType('ffn_dhcp_intent');fake.compile_intent=lambda root:dict(servers=intent)
        with patch.dict(sys.modules,{'ffn_dhcp_intent':fake}):
            calls=[]
            def rpc(resource,action='status',payload=None):
                calls.append((resource,action,payload))
                if action=='apply':return dict(applied=True)
                return dict(config=dict(revision=len([c for c in calls if c[1]=='apply']),servers=intent if any(c[1]=='apply' for c in calls) else {},configuration=None))
            status=Status();configd_applier.reconcile_dhcp(None,status,'d'*64,rpc=rpc)
            self.assertFalse(status.errors);self.assertEqual(status.applied[0][0],'network/dhcp/interface/ae1.69')
            self.assertEqual(calls[1],('dhcp','apply',dict(revision=0,servers=intent,configuration='d'*64)))
            # already applied for this configuration: nothing is sent
            calls.clear();status=Status()
            configd_applier.reconcile_dhcp(None,status,'d'*64,rpc=lambda r,a='status',p=None:(calls.append((r,a,p)) or dict(config=dict(revision=1,servers=intent,configuration='d'*64))))
            self.assertEqual([c[1] for c in calls],['status']);self.assertFalse(status.errors)
            # readback mismatch and an unreachable resource with servers committed are commit failures
            status=Status();configd_applier.reconcile_dhcp(None,status,'d'*64,rpc=lambda r,a='status',p=None:dict(config=dict(revision=1,servers={},configuration='d'*64)) if a!='apply' else {})
            self.assertEqual(status.errors[0][0],'network/dhcp');self.assertIn('readback',status.errors[0][2])
            def down(resource,action='status',payload=None):raise ValueError('MP dhcp/status request: no such resource')
            status=Status();configd_applier.reconcile_dhcp(None,status,'d'*64,rpc=down);self.assertTrue(status.errors)
            # ... but with no server committed the resource may be absent
            fake.compile_intent=lambda root:dict(servers={})
            status=Status();configd_applier.reconcile_dhcp(None,status,'d'*64,rpc=down);self.assertFalse(status.errors)
            fake.compile_intent=lambda root:(_ for _ in ()).throw(ValueError('ae1.69: pool is outside 10.1.0.0/22'))
            status=Status();configd_applier.reconcile_dhcp(None,status,'d'*64,rpc=down);self.assertIn('outside',status.errors[0][2])

    def test_new_committed_aggregate_starts_through_controller(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_bytes(b'<config/>')
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            initial=dict(revision=7,running_revision=digest,activation=dict(activation_supported=True,groups={}))
            ready=dict(initial,aggregates=[dict(ae_name='ae1',applied=True,committed=True)])
            groups=[dict(ae_name='ae1',enabled=True,errors=[])]
            with patch('configd_applier.rpc',side_effect=[{'accepted':True},ready]) as rpc:
                observed,errors=configd_applier.converge_aggregates(groups,path,initial,timeout=0)
            self.assertFalse(errors);self.assertEqual(observed,ready)
            self.assertEqual(rpc.call_args_list[0].args,('aggregates','apply',dict(
                group='ae1',operation='activate',running_revision=digest,revision=7)))

    def test_existing_owner_is_never_restarted_for_config_reconciliation(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_bytes(b'<config/>')
            for owner in ({'fresh':True},{'fresh':False,'state':'unavailable'}):
                observed=dict(revision=7,running_revision=hashlib.sha256(path.read_bytes()).hexdigest(),
                    activation=dict(activation_supported=True,groups={'ae1':owner}))
                with patch('configd_applier.rpc') as rpc:
                    configd_applier.converge_aggregates([dict(ae_name='ae1',enabled=True,errors=[])],path,observed,timeout=0)
                rpc.assert_not_called()

    def test_missing_service_and_changed_revision_do_not_activate(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_bytes(b'<config/>')
            for observed in ({'activation':{'activation_supported':False}},
                             {'running_revision':'stale','activation':{'activation_supported':True,'groups':{}}}):
                with patch('configd_applier.rpc') as rpc:
                    _,errors=configd_applier.converge_aggregates([dict(ae_name='ae1',enabled=True,errors=[])],path,observed,timeout=0)
                self.assertIn('ae1',errors);rpc.assert_not_called()

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

    def test_faceplate_apply_retries_only_a_revision_conflict_against_a_refreshed_view(self):
        """Another owner moves the faceplate revision between the applier's read and
        its apply (an aggregate preparing its members after a processor restart):
        the conflict is retried once the faceplate is re-read; any other rejection
        is not."""
        xml='''<config><devices><entry name="localhost.localdomain"><network><interface><ethernet>
        <entry name="ethernet1/1"><layer3><ip><entry name="192.0.2.1/24"/></ip></layer3></entry>
        </ethernet></interface></network></entry></devices></config>'''
        for reason in ('revision conflict; refresh state','Configuration changed; reload before applying','Copper control unavailable or pending'):
            with self.subTest(reason=reason):
                face={'revision':1,'ports':[{'port':1,'enabled':False,'available':True}]}
                net={'config':{'revision':1,'ports':{'p1':{'mode':'l3','addresses':['192.0.2.1/24']}}},'backend':{'ports':[1]}}
                calls=[];reads=[]
                def rpc(resource,action='status',payload=None):
                    calls.append((resource,action,payload))
                    if action=='status' and resource=='faceplate':
                        view=copy.deepcopy(face);reads.append(view['revision'])
                        if len(reads)==1:face['revision']=2   # moved by another owner right after the first read
                        return view
                    if action=='status':return copy.deepcopy(net)
                    if resource=='faceplate':
                        if payload['revision']!=face['revision'] or 'conflict' not in reason and 'changed' not in reason:
                            raise ValueError('MP faceplate/apply request x: rejected ('+reason+')')
                        face['ports'][0]['enabled']=payload['enabled'];face['revision']+=1
                        return {'data':copy.deepcopy(face)}
                    net['config']['ports'].update(payload['ports']);net['config']['revision']+=1
                    return {}
                with tempfile.TemporaryDirectory() as temp:
                    path=Path(temp)/'running.xml';path.write_text(xml)
                    status=Status()
                    with patch('configd_applier.rpc',side_effect=rpc),patch('configd_applier.time.sleep') as sleep:
                        if 'conflict' in reason or 'changed' in reason:
                            with self.assertLogs('ffn-configd','WARNING') as logged:PlatformApplier(path).reconcile(status)
                            self.assertIn('re-reading and retrying',logged.output[0])
                            self.assertEqual(status.errors,[])
                            self.assertTrue(face['ports'][0]['enabled'])
                            self.assertEqual(reads,[1,2])
                            self.assertEqual([p['revision'] for r,a,p in calls if (r,a)==('faceplate','apply')],[1,2])
                            sleep.assert_called_once()
                        else:
                            with self.assertRaisesRegex(ValueError,'Copper control'):PlatformApplier(path).reconcile(status)
                            self.assertEqual(len([1 for r,a,p in calls if (r,a)==('faceplate','apply')]),1)
                            sleep.assert_not_called()

    def test_routes_are_read_from_xml_and_deletions_do_not_use_legacy_sql(self):
        from xml.etree import ElementTree as ET
        dev=ET.fromstring('''<entry><network><virtual-router><ffn-candidate-managed>yes</ffn-candidate-managed>
        <entry name="default"><routing-table><ip><static-route><entry name="default"><destination>0.0.0.0/0</destination>
        <nexthop><ip-address>192.0.2.254</ip-address></nexthop><interface>ethernet1/1</interface><metric>100</metric>
        </entry></static-route></ip></routing-table></entry></virtual-router></network></entry>''')
        config={'ports':{'p1':{'mode':'l3','addresses':['192.0.2.1/24']}}}
        self.assertEqual(configd_applier.committed_routes(dev,config),[
            {'dst':'0.0.0.0/0','via':'192.0.2.254','dev':'p1','metric':100,'track_link':True,'onlink':False,'monitor':{}}])
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

    def test_route_conflicts_and_gateway_prefix_have_actionable_errors(self):
        from xml.etree import ElementTree as ET
        dev=ET.fromstring('''<entry><network><virtual-router><ffn-candidate-managed>yes</ffn-candidate-managed>
        <entry name="default"><routing-table><ip><static-route><entry name="wan"><destination>0.0.0.0/0</destination>
        <nexthop><ip-address>192.0.2.254</ip-address></nexthop><interface>ethernet1/1</interface><metric>100</metric>
        </entry></static-route></ip></routing-table></entry></virtual-router></network></entry>''')
        config={'ports':{'p1':{'mode':'l3','addresses':['192.0.2.1/32']}}}
        self.assertTrue(configd_applier.committed_routes(dev,config)[0]['track_link'])  # Stored intent; runtime withholds an unreachable next hop.
        config['ports']['p1']['addresses']=['192.0.2.1/24']
        routes=dev.find('.//static-route');routes.append(copy.deepcopy(routes[0]))
        with self.assertRaisesRegex(ValueError,'primary/backup metrics'):configd_applier.committed_routes(dev,config)
        routes[1].find('metric').text='200'
        self.assertEqual(len(configd_applier.committed_routes(dev,config)),2)

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
