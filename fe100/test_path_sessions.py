from test_flow_ids import FixtureIds
import copy
import sqlite3
import unittest
from ffn_fe100_path_owner import PathOwner
from ffn_fe100_path_sessions import PathSessions
from ffn_fe100_policy import PolicyOwner,digest
from ffn_fe100_sessions import SessionManager
from test_path_owner import Backend as Tables,plan,POOLS
from test_sessions import Backend as Flows


class CoupledPaths(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.tables=Tables();self.flows=Flows();self.manager=SessionManager(self.flows)
        self.plan=plan()
        self.resources=PathOwner(self.db,self.tables,POOLS,'boot',lambda k:copy.deepcopy(self.plan),lambda k:False)
        self.policy=PolicyOwner(self.manager,lambda:None,lambda s:None,
            lambda:{'5':dict(enabled=True,link=True),'13':dict(enabled=True,link=True)},lambda:True,nat_qualified=lambda:True,flow_ids=FixtureIds())
        self.bridge=PathSessions(self.policy,self.resources)
        self.policy.replace(0,'a'*64);self.policy.activate(1,'a'*64)
        self.request=dict(session_id=42,revision=1,policy_digest='a'*64,nat_digest=self.plan['nat_digest'],
            rule_id='test',verdict='allow',ingress=5,egress=13,inspection_required=False,nat_required=True,established=True,
            original=dict(source='192.0.2.10',destination='198.51.100.10',source_port=12345,destination_port=443,protocol=6),
            reply=dict(source='198.51.100.10',destination='203.0.113.10',source_port=443,destination_port=45000,protocol=6))
    def test_pair_uses_allocated_hops_and_removes_flows_first(self):
        self.bridge.admit(self.request,self.plan)
        self.assertEqual(len(self.flows.rows),2);self.assertEqual(len(self.tables.rows),4)
        delete=self.tables.delete
        def safe_delete(k,i):
            self.assertFalse(self.flows.rows);delete(k,i)
        self.tables.delete=safe_delete
        self.bridge.revoke(42);self.assertFalse(self.tables.rows);self.assertFalse(self.resources.paths)
    def test_neighbor_change_drains_nat_before_freeing_path(self):
        self.bridge.admit(self.request,self.plan);self.plan['directions'][0]['neighbor_revision']=digest('new')
        self.bridge.reconcile();self.assertFalse(self.flows.rows);self.assertFalse(self.tables.rows)
        self.assertFalse(self.policy.status()['admission_enabled'])
    def test_failed_flow_delete_keeps_all_resources_journaled(self):
        self.bridge.admit(self.request,self.plan);self.flows.delete=lambda k:None
        self.plan['nat_digest']=digest('changed')
        with self.assertRaises(RuntimeError):self.bridge.reconcile()
        self.assertEqual(len(self.tables.rows),4);self.assertEqual(len(self.resources.paths),1)
    def test_failed_admission_cleans_resources_and_blocks_policy(self):
        self.flows.fail=2
        with self.assertRaises(TimeoutError):self.bridge.admit(self.request,self.plan)
        self.assertFalse(self.flows.rows);self.assertFalse(self.tables.rows)
        self.assertFalse(self.policy.activated)
    def test_lifecycle_direct_revoke_is_collected_by_reconcile(self):
        self.bridge.admit(self.request,self.plan);self.policy.revoke(42)
        self.bridge.reconcile();self.assertFalse(self.tables.rows)
    def test_restart_with_lost_dependencies_drains_durable_flows_first(self):
        self.bridge.admit(self.request,self.plan);self.policy.dependencies={};self.manager.recovery_required=True
        self.bridge.recover();self.assertFalse(self.flows.rows);self.assertFalse(self.tables.rows)
    def test_unqualified_stale_or_inspected_sessions_do_not_allocate(self):
        for changes in ({'revision':0},{'verdict':'deny'},{'inspection_required':True},{'established':False}):
            with self.assertRaises(ValueError):self.bridge.admit(self.request|changes,self.plan)
        self.policy.nat_qualified=lambda:False
        with self.assertRaises(RuntimeError):self.bridge.admit(self.request,self.plan)
        self.assertFalse(self.tables.calls);self.assertFalse(self.flows.writes)


if __name__=='__main__':unittest.main()
