import base64
import hashlib
import json
import unittest
import uuid
from unittest.mock import patch
import ffn_fe100_observations as module

NONCE='11111111-1111-4111-8111-111111111111'
PRODUCER=dict(boot_id='22222222-2222-4222-8222-222222222222',pid=42,process_start='100',
              stream_id='33333333-3333-4333-8333-333333333333')
POLICY=dict(revision=3,digest='a'*64,nat_digest='b'*64,bindings={},collector={})
ROW=dict(identity='c'*64,original={},reply={},software_candidate=False,blockers=['unsupported'])


def message(sequence,operation,payload):
    return dict(schema=1,nonce=NONCE,producer=PRODUCER,sequence=sequence,operation=operation,
                emitted_monotonic=1,payload=payload)


def fragments(value):
    raw=json.dumps(value,separators=(',',':')).encode();transfer=str(uuid.uuid4())
    return [dict(nonce=NONCE,transfer=transfer,total=len(raw),offset=offset,
                 digest=hashlib.sha256(raw).hexdigest(),data=base64.b64encode(raw[offset:offset+module.CHUNK]).decode())
            for offset in range(0,len(raw),module.CHUNK)]


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.now=1;self.withdrawals=[]
        self.owner=module.Observations(lambda:self.withdrawals.append('withdrawn'),clock=lambda:self.now)
        self.owner.start(NONCE);self.send(message(1,'begin',POLICY))

    def send(self,value):
        for part in fragments(value):result=self.owner.chunk(part)
        return result

    def ready(self):
        self.send(message(2,'snapshot',[ROW]));self.send(message(3,'synchronized',{}))

    def test_complete_frame_only_advances_acknowledgement(self):
        parts=fragments(message(2,'snapshot',[ROW|{'padding':'x'*100000}]))
        for part in parts[:-1]:
            self.assertFalse(self.owner.chunk(part)['complete'])
            self.assertEqual(self.owner.status()['sequence'],1)
            self.assertEqual(self.owner.status()['sessions'],0)
        final=self.owner.chunk(parts[-1]);self.assertTrue(final['complete'])
        self.assertEqual(final['ack']['sequence'],2)
        self.assertEqual(final['ack']['sessions'],1)
        self.assertFalse(final['ack']['hardware_admission'])

    def test_duplicate_fragment_fences_without_advancing(self):
        part=fragments(message(2,'snapshot',[ROW|{'padding':'x'*100000}]))[0]
        self.owner.chunk(part);count=len(self.withdrawals)
        with self.assertRaisesRegex(ValueError,'replay'):self.owner.chunk(part)
        self.assertGreater(len(self.withdrawals),count)
        self.assertFalse(self.owner.status()['ready']);self.assertIsNone(self.owner.receiver)

    def test_fragment_expiry_and_silent_relay_withdraw_independently(self):
        self.ready();self.now=2
        self.owner.chunk(fragments(message(4,'upsert',ROW|{'padding':'x'*100000}))[0])
        self.now=7;self.owner.tick()
        self.assertIsNone(self.owner.receiver);self.assertFalse(self.owner.status()['ready'])
        self.owner.start(str(uuid.uuid4()));self.owner.receiver.last=7
        self.now=17;self.owner.tick();self.assertIsNone(self.owner.receiver)

    def test_sequence_gap_and_digest_corruption_withdraw(self):
        count=len(self.withdrawals)
        with self.assertRaisesRegex(ValueError,'sequence'):self.send(message(4,'synchronized',{}))
        self.assertGreater(len(self.withdrawals),count)
        self.owner.start(NONCE);part=fragments(message(1,'begin',POLICY))[0];part['digest']='f'*64
        with self.assertRaisesRegex(ValueError,'digest mismatch'):self.owner.chunk(part)

    def test_old_relay_cannot_close_or_write_successor(self):
        self.ready();successor=str(uuid.uuid4());self.owner.start(successor)
        count=len(self.withdrawals);self.owner.close(NONCE)
        with self.assertRaisesRegex(ValueError,'not synchronized'):self.send(message(4,'heartbeat',{}))
        self.assertEqual(len(self.withdrawals),count)
        self.assertEqual(self.owner.receiver.nonce,successor)

    def test_memory_limit_and_invalid_bounds_fence(self):
        with patch.object(module,'INVENTORY_BYTES',100):
            with self.assertRaisesRegex(ValueError,'memory limit'):self.send(message(2,'snapshot',[ROW]))
        self.assertIsNone(self.owner.receiver)
        self.owner.start(NONCE)
        part=fragments(message(1,'begin',POLICY))[0];part['total']=module.MAX_FRAME+1
        with self.assertRaisesRegex(ValueError,'bounds'):self.owner.chunk(part)

    def test_unavailable_withdraws_before_ack(self):
        self.ready();count=len(self.withdrawals)
        result=self.send(dict(schema=1,nonce=NONCE,unavailable='route change'))
        self.assertGreater(len(self.withdrawals),count)
        self.assertFalse(result['ack']['ready']);self.assertEqual(result['ack']['sessions'],0)

    def test_withdrawal_failure_is_not_acknowledged(self):
        def fail():raise RuntimeError('hardware drain failed')
        self.owner.withdraw=fail
        with self.assertRaisesRegex(RuntimeError,'drain failed'):self.owner.close(NONCE)
        self.assertFalse(self.owner.status()['ready'])

    def test_client_binds_owner_and_exact_fragment_ack(self):
        owner_id=str(uuid.uuid4());calls=[]
        def rpc(op,payload):
            calls.append(op)
            if op=='observe-start':self.owner=module.Observations(lambda:None);self.owner.start(payload['nonce']);result={}
            elif op=='observe-chunk':
                self.assertEqual(payload['control_owner'],owner_id)
                result=self.owner.chunk({k:v for k,v in payload.items() if k!='control_owner'})
            else:result=self.owner.close(payload['nonce'])
            return dict(result,control_owner=owner_id)
        client=module.ObservationClient(NONCE,rpc)
        self.assertEqual(client.send(message(1,'begin',POLICY))['sequence'],1)
        self.assertEqual(client.send(message(2,'snapshot',[ROW|{'padding':'x'*100000}]))['sequence'],2)
        client.close();self.assertFalse(self.owner.status()['ready'])
        self.assertGreater(calls.count('observe-chunk'),2)
        def replaced(op,payload):return dict(control_owner=str(uuid.uuid4()),transfer=payload['transfer'],received=1,complete=True)
        client.rpc=replaced
        with self.assertRaisesRegex(RuntimeError,'acknowledgement changed'):client.send(message(3,'synchronized',{}))


if __name__=='__main__':unittest.main()
