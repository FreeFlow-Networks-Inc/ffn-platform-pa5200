from test_flow_ids import FixtureIds
import copy
from pathlib import Path
import tempfile
import unittest
from ffn_fe100_journal import Journal
from ffn_fe100_lifecycle import SessionLifecycle
from ffn_fe100_path_owner import PathOwner
from ffn_fe100_path_sessions import PathSessions
from ffn_fe100_policy import PolicyOwner,digest
from ffn_fe100_sessions import SessionManager
from test_lifecycle import BOOT
from test_path_owner import POOLS
from test_path_sessions import CoupledPaths


class PathLifecycle(unittest.TestCase):
    def setUp(self):
        CoupledPaths.setUp(self)
        self.now=0;self.generation=digest('applied topology and policy')
        self.life=self.make_life()
        self.life.start(BOOT,1,'a'*64)
        delete=self.tables.delete
        def ordered_delete(kind,index):
            self.assertFalse(self.flows.rows,'Resources removed before all flow acknowledgements')
            delete(kind,index)
        self.tables.delete=ordered_delete

    def make_life(self):
        return SessionLifecycle(self.policy,paths=self.bridge,generation=lambda:self.generation,
            clock=lambda:self.now,idle_timeout=4,heartbeat_timeout=10,maximum_lifetime=8)

    def open(self):self.life.event(BOOT,1,'open',self.request)

    def empty(self):
        self.assertFalse(self.flows.rows);self.assertFalse(self.tables.rows)
        self.assertFalse(self.manager.sessions);self.assertFalse(self.resources.paths)

    def test_close_releases_paths_before_acknowledging(self):
        self.open();self.assertEqual(len(self.tables.rows),4)
        state=self.life.event(BOOT,2,'close',{'session_id':42})
        self.empty();self.assertEqual(state['sequence'],2)
        self.assertEqual(state['paths']['paths'],0)

    def test_idle_maximum_and_heartbeat_expiry_reclaim_resources(self):
        for reason in ('idle','maximum','heartbeat'):
            with self.subTest(reason=reason):
                self.setUp();self.open()
                if reason=='maximum':
                    for seq,now in ((2,3),(3,6)):
                        self.now=now
                        self.life.event(BOOT,seq,'refresh',dict(session_id=42,decision_digest=digest(self.request)))
                self.now={'idle':4,'maximum':8,'heartbeat':10}[reason]
                self.life.tick();self.empty()

    def test_generation_change_without_new_events_drains(self):
        self.open();self.generation=digest('new route generation')
        state=self.life.tick();self.empty()
        self.assertFalse(state['synchronized']);self.assertFalse(state['policy']['admission_enabled'])

    def test_generation_unavailable_drains(self):
        self.open();self.generation=None
        with self.assertRaises(RuntimeError):self.life.tick()
        self.empty();self.assertFalse(self.policy.activated)

    def test_neighbor_change_uses_resource_readback(self):
        self.open();self.plan['directions'][1]['neighbor_revision']=digest('replacement neighbor')
        self.life.tick();self.empty();self.assertFalse(self.life.status()['synchronized'])

    def test_policy_replacement_drains_resources_on_timer(self):
        self.open();self.policy.replace(1,'b'*64)
        self.life.tick();self.empty();self.assertFalse(self.life.status()['synchronized'])

    def test_mid_write_generation_change_cannot_ack_open(self):
        insert=self.flows.insert
        def changed(entry):
            insert(entry);self.generation=digest('route changed during native call')
        self.flows.insert=changed
        with self.assertRaises(RuntimeError):self.open()
        self.empty();self.assertEqual(self.life.sequence,0)

    def test_slow_native_call_cannot_outlive_producer_lease(self):
        insert=self.flows.insert
        def slow(entry):insert(entry);self.now=10
        self.flows.insert=slow
        with self.assertRaises(RuntimeError):self.open()
        self.empty();self.assertFalse(self.life.status()['synchronized'])

    def test_failed_delete_retains_paths_and_prevents_restart(self):
        self.open();delete=self.flows.delete;self.flows.delete=lambda key:None
        self.generation=digest('new route')
        with self.assertRaises(RuntimeError):self.life.tick()
        self.assertEqual(len(self.tables.rows),4);self.assertTrue(self.manager.recovery_required)
        with self.assertRaises(RuntimeError):self.life.start(BOOT,1,'a'*64)
        self.assertFalse(self.policy.activated)
        self.flows.delete=delete;self.life.tick();self.empty()
        self.life.start(BOOT,1,'a'*64);self.open()

    def test_failed_resource_delete_blocks_until_exact_recovery(self):
        self.open();delete=self.tables.delete;self.tables.delete=lambda k,i:None
        with self.assertRaises(RuntimeError):self.life.event(BOOT,2,'close',{'session_id':42})
        self.assertFalse(self.flows.rows);self.assertTrue(self.resources.recovery_required)
        self.assertFalse(self.policy.activated);self.assertEqual(len(self.resources.paths),1)
        self.tables.delete=delete;self.life.tick();self.empty()

    def test_resource_failure_fences_even_without_lifecycle_wrapper(self):
        self.open();self.tables.delete=lambda k,i:None
        with self.assertRaises(RuntimeError):self.bridge.revoke(42)
        self.assertFalse(self.policy.activated)

    def test_restart_loses_leases_and_recovers_durable_flows_before_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path=Path(tmp)/'owned.sqlite3'
            journal=Journal(journal_path)
            self.manager=SessionManager(self.flows,journal)
            self.resources=PathOwner(journal.db,self.tables,POOLS,'boot',lambda k:copy.deepcopy(self.plan),lambda k:False)
            saved=[None]
            def save(state):saved[0]=copy.deepcopy(state)
            self.policy=PolicyOwner(self.manager,lambda:None,save,
                lambda:{'5':dict(enabled=True,link=True),'13':dict(enabled=True,link=True)},
                lambda:True,nat_qualified=lambda:True,flow_ids=FixtureIds())
            self.bridge=PathSessions(self.policy,self.resources)
            self.policy.replace(0,'a'*64);self.life=self.make_life()
            self.life.start(BOOT,1,'a'*64);self.open();journal.close()
            journal=Journal(journal_path)
            try:
                self.manager=SessionManager(self.flows,journal)
                self.resources=PathOwner(journal.db,self.tables,POOLS,'boot',lambda k:copy.deepcopy(self.plan),lambda k:False)
                self.policy=PolicyOwner(self.manager,lambda:saved[0],save,
                    lambda:{},lambda:True,nat_qualified=lambda:True,flow_ids=FixtureIds())
                self.bridge=PathSessions(self.policy,self.resources);self.life=self.make_life()
                self.assertFalse(self.life.status()['synchronized'])
                self.life.tick();self.empty()
                self.assertFalse(journal.load())
                self.assertEqual(journal.db.execute('SELECT count(*) FROM hardware_paths').fetchone()[0],0)
            finally:journal.close()

    def test_remote_path_indices_are_not_accepted(self):
        with self.assertRaises(ValueError):self.life.event(BOOT,1,'open',self.request|{'path_digest':'c'*64})
        self.empty();self.assertFalse(self.tables.calls)

    def test_generation_change_during_activation_leaves_no_active_policy(self):
        activate=self.policy.activate
        def changed(*args):activate(*args);self.generation=digest('new generation')
        self.policy.activate=changed
        with self.assertRaises(RuntimeError):self.life.start(BOOT,1,'a'*64)
        self.empty();self.assertFalse(self.policy.activated)

    def test_path_owner_requires_trusted_generation_reader(self):
        with self.assertRaises(ValueError):SessionLifecycle(self.policy,paths=self.bridge)

    def test_readback_cannot_ack_changed_generation(self):
        self.open();reconcile=self.life.reconcile
        def changed():
            result=reconcile();self.generation=digest('changed during resource readback');return result
        self.life.reconcile=changed
        with self.assertRaises(RuntimeError):self.life.event(BOOT,2,'heartbeat',{})
        self.empty();self.assertEqual(self.life.sequence,1)

    def test_slow_readback_cannot_renew_expired_heartbeat(self):
        self.open();reconcile=self.life.reconcile
        def slow():
            result=reconcile();self.now=10;return result
        self.life.reconcile=slow
        with self.assertRaises(RuntimeError):self.life.event(BOOT,2,'heartbeat',{})
        self.empty();self.assertFalse(self.life.status()['synchronized'])


if __name__=='__main__':unittest.main()
