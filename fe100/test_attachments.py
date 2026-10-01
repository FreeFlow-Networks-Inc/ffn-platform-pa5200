import copy
import sqlite3
import struct
import unittest
from ffn_fe100_attachments import AttachmentOwner
from ffn_fe100_policy import digest
from test_path_owner import Backend


def plan(name='dynamic.77',ports=None,vlan=77):
    return dict(interface=name,binding_revision=digest('current binding'),zone=14,
                vlan=vlan,ports=ports or [7,19],miss_next_hop=90)


def encode(p,port):
    # Independent reference fixture; installed owners use the C encoder.
    lif=bytearray(36)
    struct.pack_into('>III',lif,4,0x80040000,(p['zone']<<16)|p['miss_next_hop'],port<<16)
    lif[16:26]=((4095<<38)|(63<<32)).to_bytes(10,'big')
    lif[26:36]=((p['vlan']<<38)|(port<<32)).to_bytes(10,'big')
    return dict(lif=bytes(lif),lef=struct.pack('>IIH',0x80000000|(port<<16),0,0))


class Attachments(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.backend=Backend();self.plans={'dynamic.77':plan()};self.events=[]
        self.withdraw_ack=True;self.drain_ack=True
        self.pools={'lif':[20,21,22,23],'lef':[24,25,26,27]}
        self.owner=self.make()
    def make(self,boot='boot'):
        def withdraw(name):self.events.append(('withdraw',name));return self.withdraw_ack
        def drain(name):self.events.append(('drain',name));return self.drain_ack
        return AttachmentOwner(self.db,self.backend,self.pools,boot,
            lambda n:copy.deepcopy(self.plans.get(n)),withdraw,drain,encode)
    def acquire(self):return self.owner.acquire('dynamic.77',self.plans['dynamic.77'])
    def test_member_mappings_and_vlan_are_dynamic_and_not_admission(self):
        result=self.acquire()
        self.assertEqual(result['ingress_lifs'],{'7':20,'19':21})
        self.assertEqual(result['egress_lifs'],{'7':24,'19':25})
        self.assertFalse(result['hardware_admission'])
        self.assertEqual([c[1] for c in self.backend.calls],['lef','lef','lif','lif'])
        self.owner.release('dynamic.77')
        self.assertEqual(self.events,[('withdraw','dynamic.77'),('drain','dynamic.77')])
        self.assertEqual([c[1] for c in self.backend.calls[4:]],['lif','lif','lef','lef'])
        self.assertFalse(self.backend.rows)
    def test_distinct_vlans_share_member_ports_but_selectors_cannot_overlap(self):
        self.acquire();self.plans['other']=plan('other',vlan=78)
        self.owner.acquire('other',self.plans['other'])
        self.plans['collision']=plan('collision',vlan=77)
        with self.assertRaises(ValueError):self.owner.acquire('collision',self.plans['collision'])
        self.assertEqual(len(self.backend.calls),8)
    def test_occupied_slots_are_not_adopted(self):
        self.backend.rows['lif',20]=b'foreign'
        self.assertEqual(self.acquire()['ingress_lifs'],{'7':21,'19':22})
        self.owner.release('dynamic.77');self.assertEqual(self.backend.rows,{('lif',20):b'foreign'})
    def test_pool_exhaustion_has_no_writes(self):
        self.backend.rows.update({('lef',i):b'foreign' for i in (24,25,26)})
        with self.assertRaises(RuntimeError):self.acquire()
        self.assertFalse(self.backend.calls);self.assertFalse(self.owner.rows)
    def test_member_or_binding_change_withdraws_and_drains(self):
        for field,value in [('ports',[7]),('binding_revision',digest('new binding')),('zone',99),('vlan',78)]:
            self.plans['dynamic.77']=plan();self.acquire()
            self.plans['dynamic.77'][field]=value
            self.owner.reconcile();self.assertFalse(self.owner.rows);self.assertFalse(self.backend.rows)
    def test_missing_withdraw_or_drain_ack_preserves_resources(self):
        self.acquire();before=copy.deepcopy(self.backend.rows)
        self.withdraw_ack=False
        with self.assertRaises(RuntimeError):self.owner.release('dynamic.77')
        self.assertEqual(self.events,[('withdraw','dynamic.77')]);self.assertEqual(self.backend.rows,before)
        self.withdraw_ack=True;self.drain_ack=False
        with self.assertRaises(RuntimeError):self.owner.recover()
        self.assertEqual(self.backend.rows,before)
        self.drain_ack=True;self.owner.recover();self.assertFalse(self.backend.rows)
    def test_lost_write_ack_recovery_and_restart_never_adopts_entries(self):
        self.backend.fail=('lif',20)
        with self.assertRaises(TimeoutError):self.acquire()
        self.owner=self.make()
        with self.assertRaises(RuntimeError):self.acquire()
        self.owner.recover();self.assertFalse(self.backend.rows)
    def test_foreign_hardware_and_changed_boot_are_not_deleted(self):
        self.acquire();self.owner=self.make('new boot')
        with self.assertRaises(RuntimeError):self.owner.recover()
        self.assertEqual(len(self.backend.calls),4)
        self.owner=self.make();self.backend.rows['lif',21]=b'foreign'
        with self.assertRaises(RuntimeError):self.owner.recover()
        self.assertEqual(len(self.backend.calls),4)
    def test_generation_change_during_write_leaves_durable_recovery(self):
        insert=self.backend.insert
        def changed(*args):insert(*args);self.plans['dynamic.77']['binding_revision']=digest('changed')
        self.backend.insert=changed
        with self.assertRaises(RuntimeError):self.acquire()
        self.assertTrue(self.owner.recovery_required);self.assertEqual(len(self.backend.calls),1)
        self.make().recover();self.assertFalse(self.backend.rows)
    def test_journal_tampering_or_pool_change_does_not_touch_hardware(self):
        self.acquire();self.pools['lif']=[10,11]
        with self.assertRaises(ValueError):self.make()
        self.pools['lif']=[20,21,22,23]
        self.db.execute("UPDATE hardware_attachments SET body=replace(body,'\"zone\": 14','\"zone\": 15')");self.db.commit()
        with self.assertRaises(ValueError):self.make()
        self.assertEqual(len(self.backend.calls),4)
    def test_invalid_plans_and_stale_context_have_no_writes(self):
        for change in ({'ports':[7,7]},{'ports':[True]},{'ports':[64]},{'vlan':4095},
                       {'zone':True},{'miss_next_hop':65536},{'binding_revision':'bad'}):
            with self.assertRaises(ValueError):self.owner.acquire('dynamic.77',plan()|change)
        with self.assertRaises(RuntimeError):self.owner.acquire('dynamic.77',plan()|{'zone':15})
        self.assertFalse(self.backend.calls)
    def test_nested_transaction_is_not_committed_by_owner(self):
        self.db.execute('CREATE TABLE unrelated (id INTEGER)')
        self.db.execute('INSERT INTO unrelated VALUES (1)')
        with self.assertRaises(ValueError):self.make()
        with self.assertRaises(RuntimeError):self.acquire()
        self.assertTrue(self.db.in_transaction);self.assertFalse(self.backend.calls)


if __name__=='__main__':unittest.main()
