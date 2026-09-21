import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
import uuid
import aggregate_activation as activation
from test_aggregate_config import XML


class ActivationTests(unittest.TestCase):
    def test_restart_recompiles_running_config_and_fences_previous_lifetime(self):
        for reboot in (False,True):
            with self.subTest(reboot=reboot),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);running=directory/'running.xml';running.write_bytes(XML)
                old=activation.prepare(XML,'ae1',False,str(uuid.uuid4()));old['epoch']='old-cp'
                activation.atomic(directory/'ae1-intent.json',old)
                current_boot=str(uuid.uuid4()) if reboot else old['intent']['boot_id']
                current_epoch='new-cp' if reboot else old['epoch']
                calls=[]
                def remote(role,operation,payload):
                    calls.append((role,operation,payload))
                    if operation=='fabric':return dict(ready=True,boot_id=current_boot,epoch=current_epoch)
                    if operation=='status':
                        if role=='dp':return dict(boot_id=current_boot,groups={})
                        return dict(epoch=current_epoch,groups={'ae1':dict(token=old['intent']['token'],epoch=old['epoch'],phase='active',ports=[23,24])})
                    return {}
                # The saved activation contains a different address. Only the
                # current committed XML may populate the replacement intent.
                running.write_bytes(XML.replace(b'<bond>',b'<mtu>1499</mtu><bond>'))
                with patch.object(activation,'DIRECTORY',directory),patch.object(activation,'RUNNING',running),patch('policy_guard.before_commit') as guard:
                    selected=activation.resume_selection('ae1',remote)
                self.assertEqual(selected['intent']['boot_id'],current_boot)
                self.assertNotEqual(selected['intent']['token'],old['intent']['token'])
                self.assertEqual(selected['intent']['network']['mtu'],1499)
                self.assertEqual(selected['running_revision'],activation.plan(running.read_bytes())['revision'])
                self.assertEqual(selected['epoch'],current_epoch)
                self.assertEqual(json.loads((directory/'ae1-intent.json').read_text()),selected)
                self.assertEqual([c[:2] for c in calls],[('dp','status'),('cp','status'),('cp','recover' if reboot else 'stop')]+([] if reboot else [('dp','recover')])+[('dp','fabric'),('cp','fabric')])
                guard.assert_called_once_with(running.read_bytes())

    def test_restart_never_replaces_live_or_foreign_owners_or_failed_cleanup(self):
        for scenario in ('live-dp','foreign-dp','foreign-cp','overlap','cleanup-failed','disabled','changed-config'):
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as tmp:
                directory=Path(tmp);running=directory/'running.xml';running.write_bytes(XML)
                old=activation.prepare(XML,'ae1',False,str(uuid.uuid4()));old['epoch']='cp'
                path=directory/'ae1-intent.json';activation.atomic(path,old);before=path.read_bytes()
                dp=dict(boot_id=old['intent']['boot_id'],groups={})
                cp=dict(epoch='cp',groups={'ae1':dict(token=old['intent']['token'],epoch='cp',phase='active',ports=[23,24])})
                if scenario=='live-dp':dp['groups']['ae1']=dict(fresh=True,token=old['intent']['token'])
                if scenario=='foreign-dp':dp['groups']['ae1']=dict(fresh=False,token=str(uuid.uuid4()))
                if scenario=='foreign-cp':cp['groups']['ae1']['token']=str(uuid.uuid4())
                if scenario=='overlap':cp['groups']={'ae2':dict(phase='active',ports=[23,24])}
                if scenario=='disabled':running.write_bytes(XML.replace(b'<aggregate-ethernet><entry name="ae1">',b'<aggregate-ethernet><entry name="ae1"><link-state>down</link-state>'))
                mutations=[]
                def remote(role,operation,payload):
                    if operation=='status':return dp if role=='dp' else cp
                    mutations.append((role,operation))
                    if scenario=='cleanup-failed':raise RuntimeError('withdrawal failed')
                    if scenario=='changed-config':running.write_bytes(XML+b' ')
                    return {}
                with patch.object(activation,'DIRECTORY',directory),patch.object(activation,'RUNNING',running),patch('policy_guard.before_commit') as guard:
                    with self.assertRaises((ValueError,RuntimeError)):activation.resume_selection('ae1',remote)
                self.assertEqual(path.read_bytes(),before);guard.assert_not_called()
                if scenario not in ('cleanup-failed','changed-config'):self.assertEqual(mutations,[])

    def test_stale_status_withdraws_parent_and_vlan_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            row=dict(group='ae1',pid=0,state='active',applied=True,dataplane=dict(configuration_revision='old',network_ready=True,subinterfaces=[dict(name='ae1.69',applied=True)]))
            activation.atomic(directory/'ae1-status.json',row)
            with patch.object(activation,'DIRECTORY',directory):result=activation.status()['groups']['ae1']
            self.assertFalse(result['fresh']);self.assertFalse(result['applied'])
            self.assertIsNone(result['dataplane']['configuration_revision'])
            self.assertFalse(result['dataplane']['network_ready'])
            self.assertFalse(result['dataplane']['subinterfaces'][0]['applied'])

    def test_network_and_unit_edits_do_not_change_link_identity(self):
        old=activation.plan(XML)['aggregates'][0];fingerprint=activation.link_revision(old)
        for change in (dict(network=dict(old['network'],addresses=['192.0.2.1/24'],dhcp=False)),dict(network=dict(old['network'],enabled=False,dhcp=False)),dict(lldp=False),dict(subinterfaces=[{'name':'ae1.69'}])):
            self.assertEqual(fingerprint,activation.link_revision(dict(old,**change)))
        self.assertNotEqual(fingerprint,activation.link_revision(dict(old,enabled=False)))
        self.assertNotEqual(fingerprint,activation.link_revision(dict(old,lacp=dict(old['lacp'],rate='slow'))))
        members=copy.deepcopy(old['members']);members[0]['speed']='40000'
        self.assertNotEqual(fingerprint,activation.link_revision(dict(old,members=members)))

    def test_link_only_aggregate_needs_no_parent_layer3(self):
        root=activation.parse(XML);entry=root.find('.//aggregate-ethernet/entry');l3=entry.find('layer3')
        bond=l3.find('bond');l3.remove(bond);entry.append(bond);entry.remove(l3)
        activation.ET.SubElement(entry,'aggregate-only').text='yes'
        raw=activation.ET.tostring(root)
        result=activation.prepare(raw,'ae1',False,str(uuid.uuid4()))
        self.assertFalse(result['intent']['network']['enabled']);self.assertFalse(result['intent']['network']['dhcp'])
        self.assertEqual(result['intent']['members'],[23,24])

    def test_only_inactive_unit_changes_can_preserve_parent_owner(self):
        base=XML.replace(b'</interface></network>',b'</interface><virtual-router><entry name="default"/></virtual-router></network><vsys><entry name="vsys1"><zone><entry name="LAN"><network><layer3/></network></entry></zone></entry></vsys>')
        changed=base.replace(b'</bond>',b'</bond><units><entry name="ae1.69"><tag>69</tag></entry></units>')
        changed=changed.replace(b'<entry name="vsys1">',b'<entry name="vsys1"><import><network><interface><member>ae1.69</member></interface></network></import>')
        changed=changed.replace(b'<layer3/>',b'<layer3><member>ae1.69</member></layer3>').replace(b'<entry name="default"/>',b'<entry name="default"><interface><member>ae1.69</member></interface></entry>')
        old=activation.parent_revision(base,'ae1')
        self.assertEqual(old,activation.parent_revision(changed,'ae1'))
        self.assertEqual(old,activation.parent_revision(changed.replace(b'<tag>69',b'<tag>70'),'ae1'))
        for raw in (base.replace(b'802.3ad',b'active-backup'),base.replace(b'ethernet1/24',b'ethernet1/22'),base.replace(b'<enable>yes',b'<enable>no'),base.replace(b'</config>',b'<shared><policy/></shared></config>')):
            self.assertNotEqual(old,activation.parent_revision(raw,'ae1'))

    def test_intent_is_compiled_from_committed_xml_and_not_client_network(self):
        result=activation.prepare(XML,'ae1',True,str(uuid.uuid4()))
        self.assertEqual(result['intent']['members'],[23,24])
        self.assertEqual(result['intent']['network']['management']['profile'],'')
        self.assertTrue(result['intent']['control_only'])
        self.assertFalse(result['intent']['network']['dhcp_default_route'])
        self.assertEqual(result['intent']['system'][:3],'02:')
        for raw in (XML.replace(b'ethernet1/24',b'ethernet1/4'),XML.replace(b'<layer3>',b'<layer3><mtu>9000</mtu>')):
            with self.assertRaises(ValueError):activation.prepare(raw,'ae1',False,str(uuid.uuid4()))

    def test_bad_revision_and_client_supplied_configuration_never_reach_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'running.xml';path.write_bytes(XML);remote=Mock()
            with patch.object(activation,'RUNNING',path):
                with self.assertRaises(ValueError):activation.execute('apply',dict(group='ae1',operation='activate',running_revision='bad',revision=0),remote)
                with self.assertRaises(ValueError):activation.execute('apply',dict(group='ae1',operation='activate',running_revision='bad',network={}),remote)
            remote.assert_not_called()

    def test_validate_does_not_start_units_or_program_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);running=root/'running.xml';running.write_bytes(XML)
            revision=activation.plan(XML)['revision']
            def remote(role,operation,payload):
                self.assertEqual(operation,'status');self.assertEqual(payload,{})
                return {'boot_id':str(uuid.uuid4())} if role=='dp' else {'epoch':'fixture','groups':{}}
            with patch.object(activation,'RUNNING',running),patch.object(activation,'DIRECTORY',root/'runtime'),patch.object(activation.subprocess,'run') as unit:
                result=activation.execute('validate',dict(group='ae1',operation='negotiate',running_revision=revision,revision=0),remote)
                self.assertTrue(result['validated']);self.assertTrue(result['control_only']);unit.assert_not_called()
                with self.assertRaises(ValueError):activation.execute('validate',dict(group='ae1',operation='negotiate',running_revision=revision,revision=1),remote)

    def test_unqualified_hardware_distribution_cannot_be_activated(self):
        remote=Mock()
        with self.assertRaisesRegex(ValueError,'hash distribution is not commissioned'):
            activation.execute('apply',dict(group='ae1',operation='offload',running_revision='anything',revision=0),remote)
        remote.assert_not_called()


class HardwareTests(unittest.TestCase):
    def setUp(self):
        import ffn_aggregate_hardware as hardware
        self.h=hardware;self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.patches=[patch.object(hardware,'STATE',root/'state.json'),patch.object(hardware,'LOCK',root/'lock'),patch.object(hardware,'FACEPLATE_LOCK',root/'faceplate.lock'),
            patch.object(hardware,'epoch',return_value='epoch'),patch.object(hardware,'call',side_effect=self.call),
            patch.object(hardware,'hardware',side_effect=self.hardware),patch.object(hardware.subprocess,'run')]
        for p in self.patches:p.start()
        self.enabled={34:False,35:False};self.redirect={23:0,24:0};self.events=[];self.fail=None
        self.request=dict(group='ae1',token=str(uuid.uuid4()),epoch='epoch',ports=[23,24],speeds={'23':'auto','24':'auto'})

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def call(self,request):
        if request['op']=='port.list':return {'ports':[dict(port=p,enabled=e,link=e,speed_mb=100000) for p,e in self.enabled.items()]}
        if request['op']=='port.link.status':return dict(configured_speed='auto',supported_speeds=[40000,100000])
        if request['op']=='port.set':
            self.events.append(('admin',request['port'],request['enable']))
            self.enabled[request['port']]=request['enable']
        return {'ok':True}

    def hardware(self,port,mode=0):
        self.events.append(('redirect',port,mode))
        if self.fail==(port,mode):raise RuntimeError('injected failure')
        if mode:self.redirect[port]=1 if mode==1 else 0
        return dict(bcm_port=port+11,queues=8,trunk_queues=8,header=11,destination=24 if self.redirect[port] else 0,enabled=self.redirect[port])

    def test_activation_redirects_before_enable_and_stop_disables_before_restore(self):
        result=self.h.execute('prepare',self.request)
        self.assertEqual(result['phase'],'active');self.assertTrue(all(r['up'] for r in result['links']))
        first_enable=next(i for i,e in enumerate(self.events) if e[0]=='admin' and e[2])
        self.assertTrue(all(('redirect',p,1) in self.events[:first_enable] for p in (23,24)))
        self.events=[]
        self.h.execute('stop',{k:self.request[k] for k in ('group','token','epoch')})
        first_restore=next(i for i,e in enumerate(self.events) if e[0]=='redirect' and e[2]==2)
        self.assertTrue(all(('admin',p,False) in self.events[:first_restore] for p in (34,35)))
        self.assertFalse(any(self.enabled.values()));self.assertFalse(any(self.redirect.values()))

    def test_reboot_recovery_requires_old_identity_and_proven_empty_hardware(self):
        self.h.execute('prepare',self.request)
        request=dict(group='ae1',token=self.request['token'],previous_epoch='epoch',epoch='new')
        with patch.object(self.h,'epoch',return_value='new'):
            with self.assertRaisesRegex(RuntimeError,'not been withdrawn'):self.h.execute('recover',request)
            self.enabled={34:False,35:False};self.redirect={23:0,24:0};self.events=[]
            for change in (dict(token=str(uuid.uuid4())),dict(epoch='epoch'),dict(previous_epoch='wrong')):
                with self.assertRaises(ValueError):self.h.execute('recover',dict(request,**change))
            result=self.h.execute('recover',request)
            self.assertEqual(result['phase'],'stopped')
            self.assertEqual(self.h.load()['groups']['ae1']['recovered_epoch'],'new')
            self.assertTrue(all((event[0]=='redirect' and event[2]==0) or (event[0]=='admin' and event[2] is False) for event in self.events))

    def test_reboot_recovery_withdraws_board_enabled_links_only_for_saved_owner(self):
        self.h.execute('prepare',self.request)
        self.redirect={23:0,24:0};self.events=[]
        with patch.object(self.h,'epoch',return_value='new'):
            self.h.execute('recover',dict(group='ae1',token=self.request['token'],previous_epoch='epoch',epoch='new'))
        self.assertFalse(any(self.enabled.values()))
        self.assertEqual({event[1] for event in self.events if event[0]=='admin'},{34,35})

    def test_reboot_recovery_refuses_existing_offload_trunk(self):
        cfg=dict(groups={'ae1':dict(token=self.request['token'],epoch='old',phase='active',ports=[23,24],offload=True)})
        self.h.atomic(self.h.STATE,cfg)
        request=dict(group='ae1',token=self.request['token'],previous_epoch='old',epoch='epoch')
        with patch('ffn_aggregate_bcm_lag.trunk',return_value=dict(exists=True)):
            with self.assertRaisesRegex(RuntimeError,'trunk still exists'):self.h.execute('recover',request)
        self.assertEqual(self.h.load()['groups']['ae1']['phase'],'active')

    def test_partial_prepare_rolls_back_and_wrong_token_cannot_stop_new_owner(self):
        self.fail=(24,1)
        with self.assertRaises(RuntimeError):self.h.execute('prepare',self.request)
        self.assertFalse(any(self.enabled.values()));self.assertFalse(any(self.redirect.values()))
        self.fail=None;self.h.execute('prepare',self.request)
        bad={k:self.request[k] for k in ('group','token','epoch')};bad['token']=str(uuid.uuid4())
        with self.assertRaises(ValueError):self.h.execute('stop',bad)
        self.assertTrue(all(self.enabled.values()))

    def test_watchdog_and_overlap_protection(self):
        self.h.execute('prepare',self.request)
        other=dict(self.request,group='ae2',token=str(uuid.uuid4()))
        with self.assertRaises(ValueError):self.h.execute('prepare',other)
        cfg=self.h.load();cfg['groups']['ae1']['heartbeat']-=20;self.h.atomic(self.h.STATE,cfg)
        with self.assertRaises(RuntimeError):self.h.execute('heartbeat',{k:self.request[k] for k in ('group','token','epoch')})
        self.h.execute('sweep',{})
        self.assertFalse(any(self.enabled.values()));self.assertEqual(self.h.load()['groups']['ae1']['phase'],'stopped')

    def test_brief_watchdog_lock_contention_does_not_kill_heartbeat(self):
        import fcntl,threading,time
        self.h.execute('prepare',self.request)
        with self.h.LOCK.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            release=threading.Thread(target=lambda:(time.sleep(.1),fcntl.flock(lock,fcntl.LOCK_UN)))
            release.start()
            try:result=self.h.execute('heartbeat',{k:self.request[k] for k in ('group','token','epoch')})
            finally:release.join()
        self.assertEqual(result['phase'],'active')

    def test_offload_members_follow_dp_gates_and_are_removed_after_admin_down(self):
        lag=dict(tid=1,exists=False,members=[]);events=[]
        def trunk(tid,operation='read',old=(),members=()):
            events.append((operation,dict(self.enabled)))
            if operation=='create':lag.update(exists=True,psc=9,ingress_metadata='physical-or-fixed-spa')
            if operation=='set':
                self.assertEqual(list(old),lag['members']);lag['members']=list(members)
            if operation=='destroy':lag.update(exists=False,members=[])
            return copy.deepcopy(lag)
        with patch('ffn_aggregate_bcm_lag.trunk',side_effect=trunk):
            result=self.h.execute('prepare',dict(self.request,offload=True))
            self.assertEqual(result['offload']['members'],[])
            heartbeat={k:self.request[k] for k in ('group','token','epoch')}
            result=self.h.execute('heartbeat',dict(heartbeat,egress=[24,23]))
            self.assertEqual(result['offload']['members'],[23,24])
            self.enabled[35]=False
            result=self.h.execute('heartbeat',dict(heartbeat,egress=[23,24]))
            self.assertEqual(result['offload']['members'],[])
            self.h.execute('stop',heartbeat)
            self.assertFalse(lag['exists'])
            self.assertTrue(all(not any(enabled.values()) for op,enabled in events if op=='destroy'))

    def test_offload_refuses_existing_trunk(self):
        with patch('ffn_aggregate_bcm_lag.trunk',return_value=dict(exists=True,members=[])):
            with self.assertRaises(ValueError):self.h.execute('prepare',dict(self.request,offload=True))
        self.assertFalse(any(self.enabled.values()))


if __name__=='__main__':unittest.main()
