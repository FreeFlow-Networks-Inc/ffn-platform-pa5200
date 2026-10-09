from test_flow_ids import FixtureIds
import copy
import tempfile
import unittest
import contextlib
import io
from pathlib import Path

from ffn_fe100_journal import Journal
from ffn_fe100_lifecycle import SessionLifecycle
from ffn_fe100_nat import session_pair4
from ffn_fe100_policy import PolicyOwner, digest
from ffn_fe100_sessions import SessionManager, key4, output_key4
from test_sessions import Backend
from test_nat_sessions import row, reverse
from test_lifecycle import BOOT
from test_session_adapter import Endpoint
from validate_nat_lifecycle import validate, LabBackend, fixture


class PairedPolicy(unittest.TestCase):
    def setUp(self):
        self.backend=Backend();self.manager=SessionManager(self.backend)
        self.bindings={'5':dict(enabled=True,link=True),'13':dict(enabled=True,link=True)}
        self.snapshot=dict(nat_digest='b'*64,directions=[
            dict(ingress=5,egress=13,destination='198.51.100.8',zone=4094,next_hop=31,
                 route_revision='c'*64,neighbor_revision='d'*64,attachment_revision='e'*64),
            dict(ingress=13,egress=5,destination='192.0.2.2',zone=4093,next_hop=30,
                 route_revision='f'*64,neighbor_revision='1'*64,attachment_revision='2'*64)])
        self.nat_ready=True
        self.owner=PolicyOwner(self.manager,lambda:None,lambda s:None,lambda:self.bindings,lambda:True,
            paths=lambda r:copy.deepcopy(self.snapshot),nat_qualified=lambda:self.nat_ready,flow_ids=FixtureIds())
        self.owner.replace(0,'a'*64);self.owner.activate(1,'a'*64)
        self.request=dict(session_id=42,revision=1,policy_digest='a'*64,nat_digest='b'*64,
            path_digest=digest(self.snapshot),rule_id='allow-internet',verdict='allow',
            original=row(),reply=reverse(row('203.0.113.9',sport=45000)),
            ingress=5,egress=13,inspection_required=False,nat_required=True,established=True)

    def test_forward_and_reverse_translation_use_own_ingress_zones(self):
        self.assertTrue(self.owner.admit(self.request)['nat'])
        entries=self.manager.sessions[42]['entries']
        self.assertEqual([int.from_bytes(e[2:4],'big') for e in entries],[4094,4093])
        for wire,target,zone in zip(entries,(row('203.0.113.9',sport=45000),reverse(row())),(4094,4093)):
            self.assertEqual(output_key4(wire),key4(target['source'],target['destination'],
                target['source_port'],target['destination_port'],target['protocol'],zone))
            self.assertTrue(int.from_bytes(wire[16:20],'big') & (1<<28))
        self.owner.revoke(42);self.assertFalse(self.backend.rows);self.assertFalse(self.owner.dependencies)

    def test_all_supported_translation_shapes_and_no_nat(self):
        for proto in (6,17):
            for target in (row(),row('203.0.113.9'),row(sport=45000),
                           row(dst='203.0.113.10',dport=8443),row('203.0.113.9','203.0.113.10',45000,8443)):
                self.setUp()
                original=row(proto=proto);target=dict(target,protocol=proto)
                self.snapshot['directions'][0]['destination']=target['destination']
                request=dict(self.request,original=original,reply=reverse(target),nat_required=target!=original,
                             path_digest=digest(self.snapshot))
                self.assertEqual(self.owner.admit(request)['nat'],target!=original)
                self.owner.revoke(42)

    def test_dependencies_invalidated_without_new_flow(self):
        for direction in (0,1):
            for field,value in (('next_hop',29),('zone',4000),('route_revision','3'*64),
                                ('neighbor_revision','4'*64),('attachment_revision','5'*64)):
                self.setUp();self.owner.admit(self.request)
                self.snapshot['directions'][direction][field]=value
                self.assertFalse(self.owner.reconcile()['admission_enabled'])
                self.assertFalse(self.backend.rows)

    def test_nat_change_and_lost_qualification_drain(self):
        for reason in ('generation','qualification','observer'):
            self.setUp();self.owner.admit(self.request)
            if reason=='generation':self.snapshot['nat_digest']='9'*64
            elif reason=='qualification':self.nat_ready=False
            else:self.owner.paths=lambda r:None
            self.assertEqual(self.owner.reconcile()['phase'],'blocked')
            self.assertFalse(self.backend.rows)

    def test_uncommissioned_or_stale_paths_never_write(self):
        for reason in ('absent','stale','destination','nat','qualification'):
            self.setUp()
            if reason=='absent':self.owner.paths=None
            elif reason=='stale':self.request['path_digest']='0'*64
            elif reason=='nat':self.request['nat_digest']='0'*64
            elif reason=='qualification':self.nat_ready=False
            else:
                self.snapshot['directions'][0]['destination']='203.0.113.254'
                self.request['path_digest']=digest(self.snapshot)
            with self.assertRaises((ValueError,RuntimeError)):self.owner.admit(self.request)
            self.assertEqual(self.backend.writes,0)

    def test_untrusted_decision_never_installs(self):
        for field,value in (('revision',0),('policy_digest','9'*64),('verdict','deny'),('nat_required',False),
                            ('inspection_required',True),('established',False),('nat_required',1),
                            ('egress',5),('reply',row(proto=6))):
            with self.assertRaises((ValueError,RuntimeError)):
                self.owner.admit(self.request | {field:value})
            self.assertEqual(self.backend.writes,0)

    def test_path_change_during_install_withholds_ack_and_drains(self):
        insert=self.backend.insert
        def change(entry):
            insert(entry)
            self.snapshot['directions'][0]['neighbor_revision']='6'*64
        self.backend.insert=change
        with self.assertRaisesRegex(RuntimeError,'during installation'):self.owner.admit(self.request)
        self.assertFalse(self.backend.rows);self.assertFalse(self.owner.status()['admission_enabled'])

    def test_failed_second_direction_and_delete_retry(self):
        self.backend.fail=2
        with self.assertRaises(TimeoutError):self.owner.admit(self.request)
        self.assertFalse(self.backend.rows);self.assertFalse(self.owner.dependencies)
        self.backend.fail=0;self.owner.admit(self.request)
        remove=self.backend.delete;self.backend.delete=lambda key:None
        self.snapshot['nat_digest']='9'*64
        with self.assertRaises(RuntimeError):self.owner.reconcile()
        self.assertTrue(self.manager.recovery_required)
        self.assertFalse(self.owner.status()['admission_enabled'])
        self.backend.delete=remove;self.owner.reconcile()
        self.assertFalse(self.backend.rows);self.assertFalse(self.owner.dependencies)

    def test_caller_cannot_mutate_installed_dependency(self):
        self.owner.admit(self.request);self.request['nat_digest']='9'*64
        self.assertTrue(self.owner.reconcile()['admission_enabled'])

    def test_durable_restart_drains_original_and_translated_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sessions.sqlite3';journal=Journal(path)
            try:
                self.owner.sessions=SessionManager(self.backend,journal)
                self.owner.admit(self.request)
                state=dict(self.owner.state)
            finally:journal.close()
            journal=Journal(path)
            try:
                restarted=PolicyOwner(SessionManager(self.backend,journal),lambda:state,lambda s:None,
                                      lambda:self.bindings,lambda:True,flow_ids=FixtureIds())
                self.assertFalse(restarted.status()['admission_enabled'])
                restarted.reconcile()
                self.assertFalse(self.backend.rows);self.assertFalse(journal.load())
            finally:journal.close()

    def test_nat_pair_lifecycle_close_expiry_and_dp_restart(self):
        for reason in ('close','expiry','process','stream','route'):
            self.setUp();now=[0]
            life=SessionLifecycle(self.owner,clock=lambda:now[0],idle_timeout=4)
            life.start(BOOT,1,'a'*64);life.event(BOOT,1,'open',self.request)
            if reason=='close':life.event(BOOT,2,'close',{'session_id':42})
            elif reason=='expiry':now[0]=4;life.tick()
            elif reason=='route':self.snapshot['directions'][1]['route_revision']='7'*64;life.tick()
            else:
                producer=BOOT | ({'pid':124} if reason=='process' else
                                  {'stream_id':'33333333-3333-4333-8333-333333333333'})
                with self.assertRaises(RuntimeError):life.event(producer,2,'heartbeat',{})
            self.assertFalse(self.backend.rows)

    def test_interzone_requires_explicit_zone_acknowledgement(self):
        entries=session_pair4(42,self.request['original'],self.request['reply'],[4094,4093],[31,30])
        with self.assertRaises(ValueError):self.manager.install(42,entries,1)
        with self.assertRaises(ValueError):self.manager.install(42,entries,1,lookup_zones=[4094,4094])
        self.assertEqual(self.backend.writes,0)


class TableLab(unittest.TestCase):
    def test_scoped_native_lab_exercises_each_lifecycle_trigger(self):
        endpoint=Endpoint()
        endpoint.status=lambda:dict(cp_boot_id=BOOT['boot_id'],commissioning_blockers=[],
                                   registers={'0x40428':0,'0x40450':0})
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            report=validate(endpoint,Path(directory))
        self.assertEqual(report['stage'],'completed')
        self.assertEqual(len(report['tests']),5)
        self.assertTrue(report['cleanup_verified'])
        self.assertFalse(report['production_admission']);self.assertFalse(endpoint.entries)

    def test_lab_refuses_unlisted_flow_keys_and_actions(self):
        request,_=fixture()
        entries=session_pair4(2048,request['original'],request['reply'],[4094,4093],[31,30])
        endpoint=Endpoint();backend=LabBackend(endpoint,entries)
        with self.assertRaises(ValueError):backend.fetch(bytes(16))
        with self.assertRaises(ValueError):backend.call('insert',entries[0][:-1]+b'\x01')
        self.assertFalse(endpoint.calls)


if __name__=='__main__':unittest.main()
