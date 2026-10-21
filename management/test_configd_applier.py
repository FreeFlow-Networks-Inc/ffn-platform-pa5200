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
            self.assertEqual(rpc.call_args_list[0].args,('ports','apply',dict(
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
    def test_lldp_enable_and_profile_are_accepted_but_malformed_options_are_not(self):
        from xml.etree import ElementTree as ET
        for body in ('','<lldp/>','<lldp><enable>no</enable></lldp>','<lldp><enable>yes</enable></lldp>',
                     '<lldp><enable>yes</enable><profile>custom</profile></lldp>'):
            configd_applier.validate_physical_options(ET.fromstring('<entry><layer3/>'+body+'</entry>'))
        for body in ('<lldp><enable>invalid</enable></lldp>','<lldp><enable>no</enable><enable>yes</enable></lldp>',
                     '<lldp><transmit>yes</transmit></lldp>','<lldp/><lldp/>','<layer3><unexpected/></layer3>','<layer2/>'):
            with self.assertRaises(ValueError):configd_applier.validate_physical_options(ET.fromstring('<entry>'+body+'</entry>'))

    LLDP_XML='''<config><devices><entry name="localhost.localdomain"><deviceconfig><system><hostname>fw1</hostname></system></deviceconfig>
    <network><lldp><enable>yes</enable></lldp>
    <profiles><lldp-profile><entry name="quiet"><mode>transmit-only</mode><option-tlvs><system-description><enabled>no</enabled></system-description>
    <management-address><enabled>no</enabled></management-address></option-tlvs></entry></lldp-profile></profiles>
    <interface><ethernet>
      <entry name="ethernet1/5"><comment>Verizon</comment><layer3><ip><entry name="152.193.185.10/32"/></ip></layer3><lldp><enable>yes</enable><profile>quiet</profile></lldp></entry>
      <entry name="ethernet1/2"><layer3><ip><entry name="172.19.0.1/24"/></ip></layer3><lldp><enable>no</enable></lldp></entry>
      <entry name="ethernet1/21"><aggregate-group>ae1</aggregate-group></entry>
      <entry name="ethernet1/22"><aggregate-group>ae1</aggregate-group><lldp><enable>yes</enable></lldp></entry>
    </ethernet><aggregate-ethernet><entry name="ae1"><layer3/><lldp><enable>yes</enable></lldp></entry></aggregate-ethernet></interface>
    </network></entry></devices></config>'''

    def test_compile_lldp_intent_follows_global_switch_profiles_and_aggregates(self):
        from xml.etree import ElementTree as ET
        intent=configd_applier.compile_lldp_intent(ET.fromstring(self.LLDP_XML),'08:66:1f:03:08:cd')
        self.assertTrue(intent['enabled']);self.assertEqual(intent['system'],dict(chassis_id='08:66:1f:03:08:cd',name='fw1',description='FFN-NGFW PA-5200'))
        self.assertEqual([p['port'] for p in intent['ports']],[5,21,22])
        five=intent['ports'][0]
        self.assertEqual((five['mode'],five['tlvs'],five['management'],five['description'],five['aggregate']),
                         ('transmit-only',['port-description','system-name','system-capabilities'],['152.193.185.10'],'Verizon',''))
        member=intent['ports'][1]
        self.assertEqual((member['port'],member['aggregate'],member['mode'],member['tlvs']),(21,'ae1','transmit-receive',list(configd_applier.LLDP_TLVS)))
        self.assertEqual(intent['ports'][2]['aggregate'],'ae1')
        off=configd_applier.compile_lldp_intent(ET.fromstring(self.LLDP_XML.replace('<lldp><enable>yes</enable></lldp>\n','<lldp><enable>no</enable></lldp>\n',1)),'08:66:1f:03:08:cd')
        self.assertFalse(off['enabled']);self.assertEqual(len(off['ports']),3)
        with self.assertRaisesRegex(ValueError,'does not exist'):
            configd_applier.compile_lldp_intent(ET.fromstring(self.LLDP_XML.replace('<profile>quiet</profile>','<profile>missing</profile>')),'08:66:1f:03:08:cd')

    def test_reconcile_lldp_applies_once_and_reads_back(self):
        from xml.etree import ElementTree as ET
        root=ET.fromstring(self.LLDP_XML);intent=configd_applier.compile_lldp_intent(root,'08:66:1f:03:08:cd')
        state={'config':{'revision':7,'lldp':{},'configuration':None},'agent':{'running':True}}
        calls=[]
        def rpc(resource,action='status',payload=None):
            calls.append((resource,action,payload))
            if action=='apply':
                self.assertEqual(payload['revision'],7);state['config'].update(revision=8,lldp=payload['lldp'],configuration=payload['configuration'])
                return {'applied':True}
            return state
        status=Status();configd_applier.reconcile_lldp(root,status,'acc0e09a',rpc=rpc,chassis_id='08:66:1f:03:08:cd')
        self.assertEqual([c[:2] for c in calls],[('lldp','status'),('lldp','apply'),('lldp','status')])
        self.assertEqual(calls[1][2]['lldp'],intent)
        paths=[a[0] for a in status.applied];self.assertIn('network/lldp',paths);self.assertIn('ethernet1/5/lldp',paths);self.assertEqual(status.errors,[])
        calls.clear();status=Status();configd_applier.reconcile_lldp(root,status,'acc0e09a',rpc=rpc,chassis_id='08:66:1f:03:08:cd')
        self.assertEqual([c[1] for c in calls],['status']);self.assertEqual(status.errors,[])
        state['agent']['running']=False;status=Status();configd_applier.reconcile_lldp(root,status,'acc0e09a',rpc=rpc,chassis_id='08:66:1f:03:08:cd')
        self.assertTrue(any('not running' in f[2] for f in status.errors))

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
            if resource=='ports':
                if action=='status':return dict(config=dict(revision=3),boot_id='b',dp={},cp={},aggregates=[],activation=dict(activation_supported=True,groups={}))
                self.assertEqual(payload['operation'],'sync');self.assertEqual(payload['revision'],3)
                return dict(applied=True,attached=[1],errors={},table={})
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
            with patch('configd_applier.rpc',side_effect=rpc),patch('configd_applier.reconcile_dhcp'),patch('configd_applier.reconcile_protect'),patch('configd_applier.reconcile_lldp'),patch('configd_applier.reconcile_service_routes'):PlatformApplier(path).reconcile(status)
        self.assertEqual(status.errors,[])
        self.assertEqual(net['config']['ports']['p1']['addresses'],['192.0.2.1/24'])
        self.assertEqual(net['config']['ports']['p2'],{'mode':'disabled'})
        self.assertEqual(net['config']['ports']['p3'],{'mode':'disabled'})
        self.assertEqual({p['port'] for r,a,p in calls if (r,a)==('faceplate','apply')},{2,3,24})
        self.assertTrue(face['ports'][0]['enabled'])
        # one sync for the whole faceplate, port 1 included; no per-port attach, detach or wan-path request
        self.assertEqual([(r,a) for r,a,p in calls if r=='ports'],[('ports','status'),('ports','apply')])
        self.assertFalse([r for r,a,p in calls if r in ('physical-ports','wan-path','aggregates')])

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
                    if resource=='ports':
                        return dict(config=dict(revision=1),dp={},cp={},aggregates=[],activation={}) if action=='status' else dict(applied=True,attached=[1],errors={})
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
                    with patch('configd_applier.rpc',side_effect=rpc),patch('configd_applier.time.sleep') as sleep,patch('configd_applier.reconcile_dhcp'),patch('configd_applier.reconcile_protect'),patch('configd_applier.reconcile_lldp'),patch('configd_applier.reconcile_service_routes'):
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
            if resource=='ports':
                return dict(config=dict(revision=1),dp={},cp={},aggregates=[],activation={}) if action=='status' else dict(applied=True,attached=[],errors={})
            if action=='status': return copy.deepcopy(face if resource=='faceplate' else net)
            if resource=='faceplate':
                next(p for p in face['ports'] if p['port']==payload['port'])['enabled']=payload['enabled']
                face['revision']+=1;return {'data':copy.deepcopy(face)}
            net['config']['ports'].update(payload['ports']);net['config']['revision']+=1
            return {}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_text(xml)
            status=Status()
            with patch('configd_applier.rpc',side_effect=rpc),patch('configd_applier.reconcile_dhcp'),patch('configd_applier.reconcile_protect'),patch('configd_applier.reconcile_lldp'),patch('configd_applier.reconcile_service_routes'):PlatformApplier(path).reconcile(status)
        self.assertEqual(net['config']['ports']['p1'],{'mode':'disabled'})
        self.assertEqual(net['config']['ports']['p5'],{'mode':'disabled'})
        self.assertFalse(face['ports'][1]['enabled'])
        self.assertEqual({e[0] for e in status.errors},{'ethernet1/21','ae1'})
        self.assertTrue(all(c[2]['port']==5 for c in calls if c[:2]==('faceplate','apply')))

class LinkSpeedDecisionTests(unittest.TestCase):
    """The committed link-speed is re-applied when the copper advertisement drifts from it."""
    def test_copper_auto_must_advertise_every_supported_rate(self):
        row={'media':'copper','configured_speed':'auto','supported_speeds':[100,1000,10000],'advertised_speeds':[1000,10000]}
        self.assertTrue(configd_applier.link_speed_change_required(row,'auto'))
        row['advertised_speeds']=[100,1000,10000]
        self.assertFalse(configd_applier.link_speed_change_required(row,'auto'))

    def test_copper_fixed_rate_matches_a_single_advertisement(self):
        row={'media':'copper','configured_speed':'1000','supported_speeds':[100,1000,10000],'advertised_speeds':[1000]}
        self.assertFalse(configd_applier.link_speed_change_required(row,'1000'))
        row['advertised_speeds']=[100,1000]
        self.assertTrue(configd_applier.link_speed_change_required(row,'1000'))
        self.assertTrue(configd_applier.link_speed_change_required(row,'auto'))

    def test_sfp_ports_compare_the_configured_speed_only(self):
        row={'media':'sfp','configured_speed':'1000','supported_speeds':[1000,10000]}
        self.assertFalse(configd_applier.link_speed_change_required(row,'1000'))
        self.assertTrue(configd_applier.link_speed_change_required(row,'10000'))


if __name__=='__main__': unittest.main()


class PortSyncTests(unittest.TestCase):
    """The commit reconciles every attachment through one `ports` sync; its per-port reasons reach the commit."""
    XML='<config><devices><entry name="localhost.localdomain"><network><interface><ethernet>'         '<entry name="ethernet1/1"><layer3><ip><entry name="192.0.2.1/24"/></ip></layer3></entry>'         '<entry name="ethernet1/5"><layer3><ip><entry name="203.0.113.1/29"/></ip></layer3></entry>'         '</ethernet></interface></network></entry></devices></config>'

    def reconcile(self,sync):
        face={'revision':1,'ports':[{'port':p,'enabled':True,'available':True} for p in (1,5)]}
        net={'config':{'revision':1,'ports':{}},'backend':{'ports':[]}}
        calls=[]
        def rpc(resource,action='status',payload=None):
            calls.append((resource,action,payload))
            if resource=='ports':
                if action=='status':return dict(config=dict(revision=9),dp={},cp={},aggregates=[],activation={})
                return sync(payload)
            if action=='status':return copy.deepcopy(face if resource=='faceplate' else net)
            if resource=='faceplate':return {'data':copy.deepcopy(face)}
            net['config']['ports'].update(payload['ports']);net['config']['revision']+=1
            return {}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml';path.write_text(self.XML)
            status=Status()
            # The DHCP, FFN Protect, LLDP and service-route steps have their own tests; here they must not reach the fake.
            with patch('configd_applier.rpc',side_effect=rpc),patch('configd_applier.reconcile_dhcp'),patch('configd_applier.reconcile_protect'), \
                    patch('configd_applier.reconcile_lldp'),patch('configd_applier.reconcile_service_routes'):
                PlatformApplier(path).reconcile(status)
        return status,net,calls

    def test_the_daemon_reason_for_a_port_that_did_not_attach_fails_that_port_only(self):
        def sync(payload):
            self.assertEqual(payload['revision'],9)
            return dict(applied=False,attached=[1],errors={'ethernet1/5':'DP owner: attach failed: native physical owner: No such device'})
        status,net,calls=self.reconcile(sync)
        self.assertEqual([e[0] for e in status.errors],['ethernet1/5/attachment'])
        self.assertIn('No such device',status.errors[0][2])
        self.assertEqual(net['config']['ports']['p1']['addresses'],['192.0.2.1/24'])   # the attached port is configured
        self.assertNotIn('p5',net['config']['ports'])                                   # the other one is not

    def test_a_failed_sync_fails_every_wanted_port_and_the_rest_of_the_commit_goes_on(self):
        def sync(payload):raise ValueError('MP ports/apply request x: rejected (DP port owner (ffn-portd) is not serving)')
        status,net,calls=self.reconcile(sync)
        self.assertEqual(sorted(e[0] for e in status.errors),['ethernet1/1/attachment','ethernet1/5/attachment'])
        self.assertTrue(all('not serving' in e[2] for e in status.errors))
