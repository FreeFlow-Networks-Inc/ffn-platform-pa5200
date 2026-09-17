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
