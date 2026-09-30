import copy
import sqlite3
import unittest
from ffn_fe100_path_owner import PathOwner
from ffn_fe100_policy import digest

KEY=digest('owned session')
POOLS={'smac':[30,31,32],'nexthop':[30,31,32]}


def plan():
    return dict(nat_digest=digest('nat'),directions=[dict(ingress=a,egress=b,destination=ip,
        zone=z,egress_lif=lif,source_mac=smac,destination_mac=dmac,vlan=vlan,mtu=1518,
        route_revision=digest(['route',i]),neighbor_revision=digest(['neighbor',i]),
        attachment_revision=digest(['attachment',i]))
        for i,(a,b,ip,z,lif,smac,dmac,vlan) in enumerate([
            (5,13,'198.51.100.10',4094,31,'02:00:00:00:00:01','02:00:00:00:00:02',100),
            (13,5,'192.0.2.10',4093,30,'02:00:00:00:00:03','02:00:00:00:00:04',None)])])


class Backend:
    def __init__(self):self.rows={};self.calls=[];self.fail=None
    def fetch(self,k,i):return self.rows.get((k,i))
    def insert(self,k,i,d):
        self.calls.append(('insert',k,i));self.rows[k,i]=d
        if self.fail==(k,i):raise TimeoutError('ambiguous write')
    def delete(self,k,i):self.calls.append(('delete',k,i));self.rows.pop((k,i),None)


class Paths(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.backend=Backend();self.plan=plan();self.drains=[];self.ack=True
        self.owner=self.new()
    def drain(self,key):self.drains.append(key);return self.ack
    def new(self,boot='boot'):return PathOwner(self.db,self.backend,POOLS,boot,lambda k:copy.deepcopy(self.plan),self.drain)
    def test_pair_programs_both_macs_before_hops_and_removes_after_drain(self):
        result=self.owner.acquire(KEY,self.plan)
        self.assertEqual([c[1] for c in self.backend.calls],['smac','smac','nexthop','nexthop'])
        self.assertEqual([d['next_hop'] for d in result['directions']],[30,31])
        self.owner.release(KEY)
        self.assertEqual(self.drains,[KEY]);self.assertFalse(self.backend.rows)
        self.assertEqual([c[1] for c in self.backend.calls[4:]],['nexthop','nexthop','smac','smac'])
    def test_occupied_slots_are_skipped_without_adopting_or_overwriting(self):
        self.backend.rows['nexthop',30]=b'x'*16
        self.assertEqual([d['next_hop'] for d in self.owner.acquire(KEY,self.plan)['directions']],[31,32])
        self.owner.release(KEY);self.assertEqual(self.backend.rows,{('nexthop',30):b'x'*16})
    def test_capacity_failure_has_no_writes(self):
        self.backend.rows.update({('smac',i):b'x'*8 for i in (30,31)})
        with self.assertRaises(RuntimeError):self.owner.acquire(KEY,self.plan)
        self.assertFalse(self.backend.calls);self.assertFalse(self.owner.paths)
    def test_intent_survives_ambiguous_write_and_recovery_removes_exact_resources(self):
        self.backend.fail=('nexthop',30)
        with self.assertRaises(TimeoutError):self.owner.acquire(KEY,self.plan)
        owner=self.new();self.assertTrue(owner.recovery_required)
        with self.assertRaises(RuntimeError):owner.acquire(digest('new'),self.plan)
        owner.recover();self.assertFalse(self.backend.rows);self.assertFalse(owner.paths)
    def test_restart_never_adopts_previous_ready_paths(self):
        self.owner.acquire(KEY,self.plan);owner=self.new()
        with self.assertRaises(RuntimeError):owner.snapshot(KEY)
        owner.recover();self.assertFalse(owner.paths)
    def test_dependency_change_drains_before_reclaiming(self):
        self.owner.acquire(KEY,self.plan);self.plan['directions'][1]['neighbor_revision']=digest('new')
        self.owner.reconcile();self.assertEqual(self.drains,[KEY]);self.assertFalse(self.backend.rows)
    def test_unacknowledged_flow_deletion_preserves_all_resources(self):
        self.owner.acquire(KEY,self.plan);before=copy.deepcopy(self.backend.rows);self.ack=False
        with self.assertRaises(RuntimeError):self.owner.release(KEY)
        self.assertEqual(self.backend.rows,before);self.assertTrue(self.owner.recovery_required)
    def test_changed_hardware_is_never_deleted(self):
        self.owner.acquire(KEY,self.plan);self.backend.rows['nexthop',31]=b'foreign hardware'
        with self.assertRaises(RuntimeError):self.owner.release(KEY)
        self.assertEqual(self.backend.rows['nexthop',31],b'foreign hardware')
        self.assertFalse(any(c[0]=='delete' for c in self.backend.calls))
    def test_boot_change_does_not_delete_identical_untrusted_resources(self):
        self.owner.acquire(KEY,self.plan);owner=self.new('new boot')
        with self.assertRaises(RuntimeError):owner.recover()
        self.assertEqual(len(self.backend.rows),4)
    def test_ecc_only_difference_is_accepted(self):
        result=self.owner.acquire(KEY,self.plan)
        for k in list(self.backend.rows):
            if k[0]=='nexthop':self.backend.rows[k]=b'\xff'+self.backend.rows[k][1:]
        self.assertEqual(self.owner.snapshot(KEY),result);self.owner.release(KEY)
        self.assertFalse(self.backend.rows)
    def test_stale_or_malformed_plan_has_no_writes(self):
        for mutation in [lambda p:p.update(nat_digest='bad'),lambda p:p['directions'][0].update(source_mac='00:00:00:00:00:00'),
                         lambda p:p['directions'][0].update(zone=True),lambda p:p['directions'][0].update(egress=5)]:
            p=plan();mutation(p)
            with self.assertRaises((ValueError,RuntimeError)):self.owner.acquire(KEY,p)
        p=plan();p['nat_digest']=digest('stale')
        with self.assertRaises(RuntimeError):self.owner.acquire(KEY,p)
        self.assertFalse(self.backend.calls)
    def test_journal_pool_change_cannot_delete_outside_commissioned_scope(self):
        self.owner.acquire(KEY,self.plan)
        with self.assertRaises(ValueError):PathOwner(self.db,self.backend,{'smac':[90,91],'nexthop':[90,91]},'boot',lambda k:self.plan,self.drain)
        self.assertFalse(any(c[0]=='delete' for c in self.backend.calls))


if __name__=='__main__':unittest.main()
