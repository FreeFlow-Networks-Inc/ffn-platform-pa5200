import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
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


class RelayTests(unittest.TestCase):
    def frames(self):return [message(1,'begin',POLICY),message(2,'snapshot',[ROW]),message(3,'synchronized',{}),message(4,'close',{'identity':ROW['identity']})]

    def test_mp_requires_exact_cp_ack_and_fences_on_disconnect(self):
        frames=iter(self.frames());receiver=protocol.Receiver(NONCE);out=[];last=[]
        def write(value):last.append(receiver.accept(value))
        def read():
            state=last[-1]
            return dict(schema=1,nonce=NONCE,producer=state['producer'],sequence=state['sequence'],
                        ready=state['ready'],sessions=state['sessions'],hardware_admission=False)
        with self.assertRaises(StopIteration):relay(NONCE,lambda:next(frames),write,read,out.append)
        self.assertTrue(any(r.get('cp_acknowledged') and r['ready'] and r['sessions']==1 for r in out))
        self.assertFalse(out[-1]['ready']);self.assertFalse(out[-1]['cp_acknowledged'])
        frames=iter(self.frames());out=[]
        with self.assertRaises(ValueError):relay(NONCE,lambda:next(frames),lambda m:None,lambda:{},out.append)
        self.assertFalse(out[-1]['ready'])

    def test_cp_reports_disconnected_and_never_imports_hardware(self):
        frames=iter(self.frames());reports=[];acks=[]
        with self.assertRaises(StopIteration):serve(NONCE,lambda:next(frames),acks.append,lambda r:reports.append(r.status()))
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
             patch.object(dp,'snapshot',return_value=([self.row],[])):
            dp.stream(NONCE,out.append,stop=lambda:True)
        self.assertEqual([m['operation'] for m in out],['begin','snapshot','synchronized'])
        row=out[1]['payload'][0];self.assertTrue(row['software_candidate'])
        self.assertEqual(row['translated']['source_port'],5000)
        receiver=protocol.Receiver(NONCE)
        for value in out:receiver.accept(value)
        self.assertTrue(receiver.ready);self.assertFalse(receiver.status()['hardware_admission'])

    def test_route_notification_invalidates_instead_of_being_ignored(self):
        source=object()
        with patch.object(dp.select,'select',return_value=([source],[],[])),self.assertRaises(dp.EventGap):
            dp.check_routes(source)


if __name__=='__main__':unittest.main()
