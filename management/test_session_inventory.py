import json
from pathlib import Path
import tempfile
import time
import unittest

import session_inventory as views
import session_stream as protocol

PRODUCER=dict(boot_id='22222222-2222-4222-8222-222222222222',pid=42,process_start='100',
              stream_id='33333333-3333-4333-8333-333333333333')


def row(identity,**changes):
    value=dict(id=identity,ct=1,proto=6,src='192.0.2.1',sport=1234,dst='198.51.100.1',dport=443,
               nat=dict(source=False,destination=False),rule='allow-web',vsys='vsys1',state=3,ttl=100,
               bytes=dict(c2s=10,s2c=20),packets=dict(c2s=1,s2c=2),candidate=True,blockers=[],
               l3=dict(available=False,pair=None,blockers=['no route']),since=1)
    return value | changes


class InventoryViewTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.inventory=Path(self.directory.name)/'inventory.json';self.report=Path(self.directory.name)/'relay.json'
        self.rows=[row('a'*64,since=1),row('b'*64,proto=17,state=None,candidate=False,blockers=['unsupported-protocol'],since=50,
                                            nat=dict(source=True,destination=False),xsrc='203.0.113.1',xsport=5000),
                   row('c'*64,vsys='vsys2',rule='dmz',since=20,l3=dict(available=True,pair=['lan','wan'],blockers=[]))]
        self.write()

    def write(self,ready=True,age=0,**changes):
        counters=dict(sessions=3,software_candidates=2,l3_candidates=1,hardware_admitted=0,blocked=1,tcp=2,udp=1,other=0,
                      nat_source=1,nat_destination=0,blockers={'unsupported-protocol':1})
        value=dict(schema=1,mode='observation-only',hardware_admission=False,ready=ready,sequence=9,producer=PRODUCER,
                   limits=dict(conntrack_max=262144,stream_capacity=protocol.CAPACITY),policy_revision=12,reason=None if ready else 'awaiting snapshot',
                   counters=counters,sessions=self.rows if ready else [],cp_acknowledged=ready,
                   writer=protocol.local_identity(),monotonic_time=time.monotonic()-age)
        self.inventory.write_text(json.dumps(value | changes));self.inventory.chmod(0o600)
        status=dict(schema=1,hardware_admission=False,ready=ready,cp_acknowledged=ready,configuration_digest='f'*64,
                    writer=protocol.local_identity(),monotonic_time=time.monotonic()-age)
        self.report.write_text(json.dumps(status));self.report.chmod(0o600)

    def test_summary_reports_dataplane_counters_capacity_and_acknowledgement(self):
        value=views.summary(self.report,self.inventory)
        self.assertEqual((value['sessions'],value['software_candidates'],value['l3_candidates'],value['blocked']),(3,2,1,1))
        self.assertEqual((value['max_sessions'],value['capacity_percent']),(262144,round(3/262144*100,2)))
        self.assertTrue(value['fresh'] and value['ready'] and value['cp_acknowledged'])
        self.assertEqual(value['hardware_admitted'],0);self.assertFalse(value['hardware_admission'])
        self.assertEqual(value['blockers'],{'unsupported-protocol':1});self.assertEqual(value['source'],views.SOURCE)

    def test_summary_without_a_ready_or_live_report_is_explicitly_unavailable(self):
        self.write(ready=False);value=views.summary(self.report,self.inventory)
        self.assertTrue(value['available']);self.assertFalse(value['ready']);self.assertIsNone(value['sessions'])
        self.assertEqual(value['reason'],'awaiting snapshot');self.assertEqual(value['max_sessions'],262144)
        self.write(age=protocol.INVENTORY_FRESH_SECONDS+1);value=views.summary(self.report,self.inventory)
        self.assertFalse(value['fresh']);self.assertIsNone(value['sessions']);self.assertFalse(value['cp_acknowledged'])
        value=views.summary(self.report,Path(self.directory.name)/'missing')
        self.assertFalse(value['available']);self.assertEqual(value['reason'],'session relay report unavailable')

    def test_entries_are_filtered_oldest_first_and_presented(self):
        page=views.entries(inventory=self.inventory,now=101)
        self.assertEqual([e['id'][0] for e in page['entries']],['a','c','b'])
        first=page['entries'][0]
        self.assertEqual((first['protocol'],first['state_name'],first['offload'],first['age_seconds'],first['short_id']),
                         ('tcp','ESTABLISHED','candidate',100,'a'*12))
        self.assertEqual(page['entries'][2]['state_name'],'ACTIVE');self.assertEqual(page['entries'][2]['offload'],'blocked')
        self.assertEqual(page['entries'][1]['offload'],'l3-ready')
        self.assertEqual((page['total'],page['matched'],page['counters']['sessions']),(3,3,3))
        self.assertEqual((page['sequence'],page['policy_revision']),(9,12))
        self.assertEqual([e['id'][0] for e in views.entries(vsys='vsys2',inventory=self.inventory)['entries']],['c'])
        self.assertEqual([e['id'][0] for e in views.entries(vsys=2,inventory=self.inventory)['entries']],['c'])
        self.assertEqual([e['id'][0] for e in views.entries(offload_filter='blocked',inventory=self.inventory)['entries']],['b'])
        self.assertEqual([e['id'][0] for e in views.entries(protocol='udp',inventory=self.inventory)['entries']],['b'])
        self.assertEqual([e['id'][0] for e in views.entries(query='203.0.113.1',inventory=self.inventory)['entries']],['b'])
        self.assertEqual([e['id'][0] for e in views.entries(query='dmz',inventory=self.inventory)['entries']],['c'])
        self.assertEqual([e['id'][0] for e in views.entries(query='198.51.100.1:443',limit=1,offset=1,inventory=self.inventory,now=101)['entries']],['c'])
        with self.assertRaises(ValueError):views.entries(offload_filter='hardware',inventory=self.inventory)
        self.write(ready=False);page=views.entries(inventory=self.inventory)
        self.assertEqual((page['entries'],page['total'],page['ready']),([],0,False))

    def test_breakdown_matches_the_conntrack_histogram_shape(self):
        self.rows.append(row('d'*64,state=1));self.write()
        self.assertEqual(views.breakdown(self.inventory),dict(tcp_established=2,tcp_other=1,udp=1,icmp=0,other=0))
        self.write(ready=False);self.assertEqual(sum(views.breakdown(self.inventory).values()),0)

    def test_entry_by_identity_or_unique_prefix(self):
        value=views.entry('a'*64,inventory=self.inventory,now=11)
        self.assertEqual(value['session']['id'],'a'*64);self.assertEqual(value['session']['age_seconds'],10)
        self.assertEqual(views.entry('bbbbbbbb',inventory=self.inventory)['session']['protocol'],'udp')
        self.assertIsNone(views.entry('ffffffff',inventory=self.inventory)['session'])
        for bad in ('abc','zzzzzzzz',None,7):
            with self.assertRaises(ValueError):views.entry(bad,inventory=self.inventory)
        self.rows.append(row('a'*63+'f'));self.write()
        with self.assertRaises(ValueError):views.entry('aaaaaaaa',inventory=self.inventory)


if __name__=='__main__':unittest.main()
