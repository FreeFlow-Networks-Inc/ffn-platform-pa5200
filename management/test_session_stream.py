import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import session_stream as protocol
from session_relay import relay
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'fe100'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'octeon/debian'))
from ffn_fe100_session_stream import serve
import ffn_session_stream_dp as dp


NONCE=str(uuid.uuid4())
PRODUCER=dict(boot_id=str(uuid.uuid4()),pid=42,process_start='100',stream_id=str(uuid.uuid4()))
POLICY=dict(revision=3,digest='a'*64,nat_digest='b'*64,bindings={},collector={})
ROW=dict(identity='c'*64,original={},reply={},software_candidate=False,blockers=['unsupported'])


def message(sequence,operation,payload,producer=PRODUCER,now=1):
    return dict(schema=1,nonce=NONCE,producer=producer,sequence=sequence,operation=operation,
                emitted_monotonic=now,payload=payload)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now=1;self.receiver=protocol.Receiver(NONCE,clock=lambda:self.now)
        self.receiver.accept(message(1,'begin',POLICY))

    def synchronized(self):
        self.receiver.accept(message(2,'snapshot',[ROW]));self.receiver.accept(message(3,'synchronized',{}))

    def test_complete_snapshot_then_ordered_removal(self):
        self.receiver.accept(message(2,'snapshot',[ROW]));self.assertFalse(self.receiver.ready)
        self.receiver.accept(message(3,'synchronized',{}));self.assertTrue(self.receiver.ready)
        self.receiver.accept(message(4,'close',{'identity':ROW['identity']}));self.assertFalse(self.receiver.sessions)
        self.receiver.accept(message(5,'upsert',ROW));self.assertEqual(len(self.receiver.sessions),1)
        self.assertFalse(self.receiver.status()['hardware_admission'])

    def test_gap_duplicate_identity_restart_nonce_and_timestamp_fence(self):
        for change in ({'sequence':5},{'sequence':3},{'nonce':str(uuid.uuid4())},
                       {'producer':PRODUCER | {'pid':43}},{'producer':PRODUCER | {'process_start':'101'}},
                       {'emitted_monotonic':0},{'emitted_monotonic':float('nan')},
                       {'emitted_monotonic':100},{'sequence':True}):
            self.setUp();self.synchronized()
            with self.assertRaises((ValueError,TimeoutError)):
                self.receiver.accept(message(4,'heartbeat',{}) | change)
            self.assertFalse(self.receiver.ready);self.assertFalse(self.receiver.sessions)

    def test_snapshot_duplicate_and_partial_never_become_ready(self):
        with self.assertRaises(ValueError):self.receiver.accept(message(2,'snapshot',[ROW,ROW]))
        self.assertFalse(self.receiver.ready);self.assertFalse(self.receiver.sessions)
        self.setUp()
        with self.assertRaises(ValueError):self.receiver.accept(message(2,'heartbeat',{}))

    def test_dead_producer_and_slow_backlog_are_not_kept_fresh(self):
        self.synchronized();self.now=11
        with self.assertRaises(TimeoutError):self.receiver.tick()
        self.assertFalse(self.receiver.ready)
        self.setUp();self.synchronized()
        self.now=9;self.receiver.accept(message(4,'heartbeat',{}))
        self.now=12
        with self.assertRaises(ValueError):self.receiver.accept(message(5,'heartbeat',{}))
        self.assertFalse(self.receiver.ready)

    def test_backlog_budget_is_separate_from_the_silence_budget(self):
        """A producer whose snapshot was buffered in one burst may run ahead of receipt by the backlog
        budget while silence between messages is still bounded by the shorter timeout."""
        self.receiver=protocol.Receiver(NONCE,clock=lambda:self.now,backlog=90)
        self.receiver.accept(message(1,'begin',POLICY));self.synchronized()
        self.now=9;self.receiver.accept(message(4,'heartbeat',{}))
        self.now=12;self.receiver.accept(message(5,'heartbeat',{}))   # 12 s behind the producer: within 90
        self.assertTrue(self.receiver.ready)
        self.now=23
        with self.assertRaises(TimeoutError):self.receiver.tick()    # 11 s of silence still expires
        self.now=1;self.receiver=protocol.Receiver(NONCE,clock=lambda:self.now,backlog=90)
        self.receiver.accept(message(1,'begin',POLICY));self.synchronized()
        self.now=9;self.receiver.accept(message(4,'heartbeat',{}))
        self.now=18;self.receiver.accept(message(5,'heartbeat',{}));self.now=27
        self.receiver.accept(message(6,'heartbeat',{}));self.now=36;self.receiver.accept(message(7,'heartbeat',{}))
        for seq,now in ((8,45),(9,54),(10,63),(11,72),(12,81),(13,90)):
            self.now=now;self.receiver.accept(message(seq,'heartbeat',{}))
        self.now=99
        with self.assertRaises(ValueError):self.receiver.accept(message(14,'heartbeat',{}))   # 99 s behind: over 90
        self.assertFalse(self.receiver.ready)

    def test_new_snapshot_clears_prior_inventory_and_changed_nat(self):
        self.synchronized()
        self.receiver.accept(message(1,'begin',POLICY | {'nat_digest':'d'*64},
                                     producer=PRODUCER | {'stream_id':str(uuid.uuid4())}))
        self.assertFalse(self.receiver.ready);self.assertFalse(self.receiver.sessions)
        self.assertEqual(self.receiver.policy['nat_digest'],'d'*64)

    def test_unavailable_is_acknowledged_without_reusing_previous_sessions(self):
        self.synchronized()
        value=self.receiver.accept(dict(schema=1,nonce=NONCE,unavailable='NAT acknowledgement changed'))
        self.assertFalse(value['ready']);self.assertEqual(value['sessions'],0)
        self.assertIsNone(value['producer']);self.assertIsNone(value['policy'])
        self.assertEqual(value['reason'],'NAT acknowledgement changed')
        self.receiver.accept(message(1,'begin',POLICY,producer=PRODUCER | {'stream_id':str(uuid.uuid4())}))
        self.assertFalse(self.receiver.ready)

    def test_oversized_inventory_fences(self):
        self.synchronized()
        with patch.object(protocol,'CAPACITY',1),self.assertRaises(ValueError):
            self.receiver.accept(message(4,'upsert',ROW | {'identity':'d'*64}))
        self.assertFalse(self.receiver.sessions)

    def test_framing_partial_eof_invalid_json_and_output_limit(self):
        read,write=os.pipe()
        try:
            protocol.send(write,{'a':1});protocol.send(write,{'b':2})
            lines=protocol.Lines(read)
            self.assertEqual(lines.read(),{'a':1});self.assertEqual(lines.read(),{'b':2})
            os.write(write,b'{');os.close(write);write=None
            with self.assertRaises(EOFError):lines.read(.1)
        finally:
            os.close(read)
            if write is not None:os.close(write)
        with self.assertRaises(ValueError):protocol.send(1,{'data':'x'*protocol.MAX_FRAME})

    def test_stale_status_and_wrong_writer_do_not_show_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json';writer=protocol.local_identity()
            path.write_text(json.dumps(dict(schema=1,writer=writer,monotonic_time=0,ready=True,hardware_admission=False)))
            path.chmod(0o600)
            self.assertFalse(protocol.read_status(path)['ready'])

    def test_changes_batch_applies_in_order_after_synchronization(self):
        other=ROW | {'identity':'d'*64,'software_candidate':True,'blockers':[]}
        with self.assertRaises(ValueError):
            self.receiver.accept(message(2,'changes',[{'op':'upsert','row':ROW}]))
        self.setUp();self.synchronized()
        state=self.receiver.accept(message(4,'changes',[{'op':'upsert','row':other},{'op':'close','identity':ROW['identity']},
                                                        {'op':'upsert','row':other | {'blockers':['late'],'software_candidate':False}}]))
        self.assertEqual(set(self.receiver.sessions),{'d'*64});self.assertEqual(state['sessions'],1)
        self.assertEqual(state['software_candidates'],0);self.assertTrue(state['ready'])
        for payload in ([],[{'op':'upsert','row':ROW}]*(protocol.BATCH+1),{},[{'op':'drop','identity':'c'*64}],
                        [{'op':'close','identity':'c'*64,'row':ROW}],[{'op':'upsert','row':ROW,'identity':'c'*64}],[['upsert',ROW]]):
            self.setUp();self.synchronized()
            with self.assertRaises(ValueError):self.receiver.accept(message(4,'changes',payload))
            self.assertFalse(self.receiver.ready);self.assertFalse(self.receiver.sessions)

    def test_candidate_counters_stay_exact_across_replace_close_and_fence(self):
        self.synchronized()
        candidate=ROW | {'identity':'d'*64,'software_candidate':True,'blockers':[]}
        self.receiver.accept(message(4,'upsert',candidate));self.receiver.accept(message(5,'upsert',candidate))
        self.assertEqual(self.receiver.status()['software_candidates'],1)
        self.receiver.accept(message(6,'upsert',candidate | {'software_candidate':False,'blockers':['x']}))
        self.assertEqual(self.receiver.status()['software_candidates'],0)
        self.receiver.accept(message(7,'upsert',candidate));self.receiver.accept(message(8,'close',{'identity':'d'*64}))
        self.assertEqual((self.receiver.status()['software_candidates'],self.receiver.status()['sessions']),(0,1))
        self.receiver.accept(message(9,'upsert',candidate))
        self.receiver.fence('test');self.assertEqual(self.receiver.status()['software_candidates'],0)
        self.assertEqual(self.receiver.inventory(),[])

    def test_begin_limits_are_optional_and_validated(self):
        self.assertIsNone(self.receiver.status()['limits'])
        limits=dict(conntrack_max=262144,stream_capacity=protocol.CAPACITY)
        self.receiver=protocol.Receiver(NONCE,clock=lambda:self.now)
        self.receiver.accept(message(1,'begin',POLICY | {'limits':limits}))
        self.assertEqual(self.receiver.status()['limits'],limits)
        self.assertNotIn('limits',self.receiver.policy)   # the policy digest context is unchanged
        for bad in ({'conntrack_max':1},{'conntrack_max':-1,'stream_capacity':1},{'conntrack_max':'1','stream_capacity':1},[],None):
            self.receiver=protocol.Receiver(NONCE,clock=lambda:self.now)
            with self.assertRaises(ValueError):self.receiver.accept(message(1,'begin',POLICY | {'limits':bad}))
        with self.assertRaises(ValueError):
            protocol.Receiver(NONCE,clock=lambda:self.now).accept(message(1,'begin',POLICY | {'extra':1}))

    def test_compact_rows_carry_tuples_counters_and_verdicts(self):
        row=dict(identity='e'*64,conntrack_id=7,token=9,rule=dict(scope='vsys1',name='allow-web'),
                 original=dict(source='192.0.2.1',destination='198.51.100.1',source_port=1234,destination_port=443,protocol=6),
                 reply=dict(source='198.51.100.1',destination='203.0.113.1',source_port=443,destination_port=5000,protocol=6),
                 translated=dict(source='203.0.113.1',destination='198.51.100.1',source_port=5000,destination_port=443,protocol=6),
                 nat=dict(source=True,destination=False),counters={'original':{'packets':4,'bytes':240},'reply':{'packets':3,'bytes':180}},
                 remaining_seconds=120,tcp_state=3,software_candidate=True,blockers=[],
                 l3=dict(available=True,hardware_admission=False,snapshot_digest='d'*64,blockers=[],pair=['lan','wan'],directions=[{},{}]))
        value=protocol.compact(row,since=5)
        self.assertEqual((value['src'],value['sport'],value['dst'],value['dport'],value['proto']),('192.0.2.1',1234,'198.51.100.1',443,6))
        self.assertEqual((value['xsrc'],value['xsport']),('203.0.113.1',5000))
        self.assertEqual(value['bytes'],{'c2s':240,'s2c':180});self.assertEqual(value['packets']['c2s'],4)
        self.assertEqual((value['rule'],value['vsys'],value['state'],value['ttl'],value['since']),('allow-web','vsys1',3,120,5))
        self.assertEqual(value['l3'],dict(available=True,pair=['lan','wan'],blockers=[]))
        self.assertTrue(value['candidate']);self.assertNotIn('directions',json.dumps(value))
        plain=protocol.compact(ROW)
        self.assertNotIn('xsrc',plain);self.assertIsNone(plain['l3']);self.assertEqual(plain['blockers'],['unsupported'])
        self.synchronized();self.now=3
        self.receiver.accept(message(4,'upsert',{k:v for k,v in row.items() if k!='l3'}))   # POLICY has no L3 generation
        self.assertEqual([r['since'] for r in self.receiver.inventory() if r['id']=='e'*64],[3])

    def test_inventory_reader_requires_live_writer_fresh_report_and_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'inventory.json';writer=protocol.local_identity()
            def write(**changes):
                value=dict(schema=1,writer=writer,monotonic_time=time.monotonic(),ready=True,hardware_admission=False,
                           sessions=[protocol.compact(ROW,since=1)],counters={'sessions':1},limits=None,sequence=4,
                           policy_revision=3,reason=None,producer=PRODUCER)
                path.write_text(json.dumps(value | changes));path.chmod(0o600)
            write();value=protocol.read_inventory(path)
            self.assertTrue(value['fresh'] and value['ready']);self.assertEqual(len(value['sessions']),1)
            write(monotonic_time=time.monotonic()-protocol.INVENTORY_FRESH_SECONDS-1)
            self.assertFalse(protocol.read_inventory(path)['fresh']);self.assertEqual(protocol.read_inventory(path)['sessions'],[])
            write(ready=False);value=protocol.read_inventory(path)
            self.assertTrue(value['fresh']);self.assertFalse(value['ready']);self.assertEqual(value['sessions'],[])
            write(writer=writer | {'process_start':'1'});self.assertFalse(protocol.read_inventory(path)['fresh'])
            write(hardware_admission=True);self.assertFalse(protocol.read_inventory(path)['available'])
            path.chmod(0o644);self.assertFalse(protocol.read_inventory(path)['available'])


class RelayTests(unittest.TestCase):
    def frames(self):return [message(1,'begin',POLICY),message(2,'snapshot',[ROW]),message(3,'synchronized',{}),message(4,'close',{'identity':ROW['identity']})]

    def test_mp_requires_exact_cp_ack_and_fences_on_disconnect(self):
        frames=iter(self.frames());receiver=protocol.Receiver(NONCE);out=[];last=[]
        def write(value):last.append(receiver.accept(value))
        def read():
            state=last[-1]
            return dict(schema=1,nonce=NONCE,producer=state['producer'],sequence=state['sequence'],
                        ready=state['ready'],sessions=state['sessions'],hardware_admission=False)
        quiet=dict(path='/nonexistent',write=lambda path,value:None)
        with self.assertRaises(StopIteration):relay(NONCE,lambda:next(frames),write,read,out.append,inventory=quiet)
        self.assertTrue(any(r.get('cp_acknowledged') and r['ready'] and r['sessions']==1 for r in out))
        self.assertFalse(out[-1]['ready']);self.assertFalse(out[-1]['cp_acknowledged'])
        frames=iter(self.frames());out=[]
        with self.assertRaises(ValueError):relay(NONCE,lambda:next(frames),lambda m:None,lambda:{},out.append,inventory=quiet)
        self.assertFalse(out[-1]['ready'])

    def test_relay_publishes_a_rate_limited_inventory_beside_its_status(self):
        from session_relay import InventoryPublisher
        receiver=protocol.Receiver(NONCE,clock=lambda:1);writes=[]
        clock=[0]
        publisher=InventoryPublisher(receiver,path='/nonexistent',clock=lambda:clock[0],write=lambda path,value:writes.append(value))
        self.assertTrue(publisher.publish(force=True));self.assertFalse(writes[-1]['ready']);self.assertEqual(writes[-1]['sessions'],[])
        receiver.accept(message(1,'begin',POLICY | {'limits':dict(conntrack_max=10,stream_capacity=protocol.CAPACITY)}))
        receiver.accept(message(2,'snapshot',[ROW]));receiver.accept(message(3,'synchronized',{}))
        self.assertFalse(publisher.publish())                     # within the interval
        clock[0]=2;self.assertTrue(publisher.publish(dict(cp_acknowledged=True)))
        value=writes[-1];self.assertTrue(value['ready']);self.assertEqual(value['counters']['sessions'],1)
        self.assertEqual(value['counters']['blockers'],{'unsupported':1});self.assertEqual(value['limits']['conntrack_max'],10)
        self.assertEqual(value['sessions'][0]['id'],ROW['identity']);self.assertTrue(value['cp_acknowledged'])
        self.assertFalse(value['hardware_admission']);self.assertEqual(value['counters']['hardware_admitted'],0)
        clock[0]=4.5;receiver.accept(message(4,'heartbeat',{}))
        self.assertFalse(publisher.publish())                     # nothing changed, heartbeat not due
        clock[0]=7.5;self.assertTrue(publisher.publish())         # the heartbeat keeps a quiet table fresh
        self.assertEqual(writes[-1]['counters']['sessions'],1)
        clock[0]=8;receiver.accept(message(5,'close',{'identity':ROW['identity']}))
        self.assertFalse(publisher.publish());clock[0]=10;self.assertTrue(publisher.publish())
        self.assertEqual(writes[-1]['sessions'],[])
        # An unwritable report path is recorded, not raised: the relay keeps the CP synchronized.
        def refuse(path,value):raise PermissionError('read-only /run')
        failing=InventoryPublisher(receiver,path='/nonexistent',clock=lambda:clock[0],write=refuse)
        self.assertFalse(failing.publish(force=True));self.assertIn('read-only',failing.error)

    def test_mp_relay_writes_inventory_through_its_loop(self):
        frames=iter(self.frames());receiver=protocol.Receiver(NONCE);out=[];last=[];inventories=[]
        def write(value):last.append(receiver.accept(value))
        def read():
            state=last[-1]
            return dict(schema=1,nonce=NONCE,producer=state['producer'],sequence=state['sequence'],
                        ready=state['ready'],sessions=state['sessions'],hardware_admission=False)
        with self.assertRaises(StopIteration):
            relay(NONCE,lambda:next(frames),write,read,out.append,
                  inventory=dict(path='/nonexistent',interval=0,heartbeat=0,write=lambda path,value:inventories.append(value)))
        self.assertTrue(any(v['ready'] and v['counters']['sessions']==1 for v in inventories))
        self.assertFalse(inventories[-1]['ready']);self.assertEqual(inventories[-1]['sessions'],[])
        self.assertFalse(inventories[-1]['cp_acknowledged'])

    def test_cp_reports_disconnected_and_never_imports_hardware(self):
        frames=iter(self.frames());reports=[];acks=[]
        class Bridge:
            def __init__(self,nonce):self.receiver=protocol.Receiver(nonce)
            def send(self,message):return protocol.acknowledgement(NONCE,self.receiver.accept(message))
            def close(self):self.receiver.fence('closed')
        with self.assertRaises(StopIteration):serve(NONCE,lambda:next(frames),acks.append,lambda r:reports.append(r.status()),Bridge)
        self.assertEqual([a['sequence'] for a in acks],[1,2,3,4])
        self.assertFalse(reports[-1]['ready']);self.assertEqual(reports[-1]['sessions'],0)
        self.assertTrue(all(r['hardware_admission'] is False for r in reports))


class DPTests(unittest.TestCase):
    def setUp(self):
        self.row=dict(id=42,token=9,event='update',status=14,timeout=60,zone=0,tcp_state=3,
            original=dict(source='192.0.2.1',destination='198.51.100.1',source_port=1234,destination_port=443,protocol=6),
            reply=dict(source='198.51.100.1',destination='203.0.113.1',source_port=443,destination_port=5000,protocol=6),counters={})
        self.state=dict(revision=3,digest='a'*64,nat={'digest':'b'*64},bindings={})
        self.collector=dict(boot_id=PRODUCER['boot_id'],pid=3,process_start='3',reconciliations=1)
        self.rules={9:dict(interface_pairs=[['lan','wan']],inspection_required=False)}

    def test_snapshot_replay_does_not_resurrect_deleted_sessions(self):
        self.assertEqual(dp.replay([self.row],[self.row | {'event':'end','token':None}]),[])

    def test_dp_bootstrap_is_complete_and_preserves_kernel_nat_tuple(self):
        out=[]
        with patch.object(dp.feed,'context',return_value=(self.state,self.collector,self.rules)), \
             patch.object(dp.feed.runtime,'saved',return_value=self.state), \
             patch.object(dp.feed.runtime,'status',return_value={}), \
             patch.object(dp.feed,'acknowledgement',return_value=self.collector), \
             patch.object(dp,'route_watch',return_value=contextlib.nullcontext(object())), \
             patch.object(dp,'check_routes'),patch.object(dp,'subscribe',return_value=contextlib.nullcontext(object())), \
             patch.object(dp.feed.l3,'snapshot',return_value=dict(links=[],addresses=[],routes=[],rules=[],neighbors=[])), \
             patch.object(dp,'snapshot',return_value=([self.row],[])):
            dp.stream(NONCE,out.append,stop=lambda:True)
        self.assertEqual([m['operation'] for m in out],['begin','snapshot','synchronized'])
        row=out[1]['payload'][0];self.assertTrue(row['software_candidate'])
        self.assertEqual(row['translated']['source_port'],5000)
        receiver=protocol.Receiver(NONCE)
        for value in out:receiver.accept(value)
        self.assertTrue(receiver.ready);self.assertFalse(receiver.status()['hardware_admission'])
        self.assertTrue(receiver.status()['l3_observed'])
        self.assertEqual(receiver.status()['l3_candidates'],0)
        self.assertEqual(row['l3']['snapshot_digest'],out[0]['payload']['l3']['snapshot_digest'])

    def test_dp_live_bursts_leave_as_coalesced_change_batches_with_limits(self):
        out=[];stops=iter([False,False,True]);events=iter([[self.row,self.row | {'id':43},self.row | {'id':43,'event':'end'}],
                                                             [self.row | {'id':44}]])
        def receive(source):
            try:return next(events)
            except StopIteration:raise BlockingIOError
        with tempfile.TemporaryDirectory() as directory:
            limits=Path(directory)/'nf_conntrack_max';limits.write_text('262144\n')
            with patch.object(dp.feed,'context',return_value=(self.state,self.collector,self.rules)), \
                 patch.object(dp.feed.runtime,'saved',return_value=self.state), \
                 patch.object(dp.feed.runtime,'status',return_value={}), \
                 patch.object(dp.feed,'acknowledgement',return_value=self.collector), \
                 patch.object(dp,'route_watch',return_value=contextlib.nullcontext(object())), \
                 patch.object(dp,'check_routes'),patch.object(dp,'subscribe',return_value=contextlib.nullcontext(object())), \
                 patch.object(dp.feed.l3,'snapshot',return_value=dict(links=[],addresses=[],routes=[],rules=[],neighbors=[])), \
                 patch.object(dp,'snapshot',return_value=([],[])),patch.object(dp,'CONNTRACK_MAX',limits), \
                 patch.object(dp,'receive',receive),patch.object(dp.select,'select',lambda r,w,x,t:([r[0]],[],[])):
                dp.stream(NONCE,out.append,stop=lambda:next(stops))
        # Both event batches drain within one 50 ms burst, so they leave as one ordered `changes` message.
        self.assertEqual([m['operation'] for m in out],['begin','synchronized','changes'])
        self.assertEqual(out[0]['payload']['limits'],dict(conntrack_max=262144,stream_capacity=protocol.CAPACITY))
        first=out[2]['payload']
        # 42 and 44 upserted; 43 created then ended inside the burst collapses to its close.
        self.assertEqual([e['op'] for e in first],['upsert','close','upsert'])
        receiver=protocol.Receiver(NONCE)
        for value in out:receiver.accept(value)
        self.assertEqual(receiver.status()['sessions'],2);self.assertEqual(receiver.status()['limits']['conntrack_max'],262144)
        self.assertEqual(dp.table_limits(Path(directory)/'missing'),None)

    def test_route_notification_invalidates_instead_of_being_ignored(self):
        # Without a reference topology any notification ends the generation; the queue is drained first.
        class Source:
            reads=0
            def recv(self,size):
                self.reads+=1
                if self.reads>1:raise BlockingIOError
                return b'notification'
        source=Source()
        with patch.object(dp.select,'select',return_value=([source],[],[])),self.assertRaises(dp.EventGap):
            dp.check_routes(source)
        self.assertEqual(source.reads,2)


if __name__=='__main__':unittest.main()
