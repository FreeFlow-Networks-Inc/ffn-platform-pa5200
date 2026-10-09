import contextlib
import copy
import unittest
from unittest.mock import patch

from test_session_stream import NONCE,PRODUCER,POLICY,ROW,message,DPTests
import session_stream as protocol
import ffn_session_stream_dp as dp
from session_relay import relay


def fixture():
    policy=copy.deepcopy(POLICY)
    policy['l3']=dict(snapshot_digest='d'*64,hardware_admission=False)
    policy['bindings']={n:dict(device=n,index=i,alias='owner-'+n) for i,n in enumerate(('lan','wan'),1)}
    row=dict(ROW,software_candidate=True,blockers=[],rule={'interface_pairs':[['lan','wan']]},
             original={'source':'192.0.2.1'},translated={'destination':'198.51.100.1'})
    directions=[]
    for name,destination in [('wan','198.51.100.1'),('lan','192.0.2.1')]:
        directions.append(dict(policy['bindings'][name],interface=name,destination=destination,
            next_hop=destination,source_mac='02:00:00:00:00:01',destination_mac='02:00:00:00:00:02',
            mtu=1500,vlan=None,decrement_ttl=True,exceptions=['ttl-expired','mtu-exceeded','ipv4-fragments']))
    row['l3']=dict(available=True,hardware_admission=False,snapshot_digest='d'*64,blockers=[],directions=directions)
    return policy,row


class L3ReceiverTests(unittest.TestCase):
    def receiver(self,policy):
        r=protocol.Receiver(NONCE,clock=lambda:1)
        r.accept(message(1,'begin',policy));return r

    def test_generation_bound_next_hops_acknowledged_and_withdrawn(self):
        policy,row=fixture();r=self.receiver(policy)
        r.accept(message(2,'snapshot',[row]));self.assertFalse(r.status()['l3_observed'])
        state=r.accept(message(3,'synchronized',{}))
        self.assertEqual(state['l3_candidates'],1);self.assertTrue(state['l3_observed'])
        ack=protocol.acknowledgement(NONCE,state)
        self.assertEqual(ack['l3'],dict(snapshot_digest='d'*64,observed=True,candidates=1))
        r.accept(dict(schema=1,nonce=NONCE,unavailable='neighbor changed'))
        self.assertEqual(r.status()['l3_candidates'],0);self.assertFalse(r.status()['l3_observed'])

    def test_bad_generation_direction_mac_and_owner_fence_entire_inventory(self):
        for field,value in [('snapshot_digest','e'*64),('hardware_admission',True),('available',1),
                            ('directions',[]),('blockers',['stale'])]:
            policy,row=fixture();r=self.receiver(policy);row['l3'][field]=value
            with self.assertRaises(ValueError):r.accept(message(2,'snapshot',[row]))
            self.assertFalse(r.ready);self.assertFalse(r.sessions)
        for field,value in [('index',8),('device','different'),('alias','replaced'),('mtu',True),
                            ('decrement_ttl',False),('destination','203.0.113.1'),('vlan',4095),
                            ('destination_mac','ff:ff:ff:ff:ff:ff')]:
            policy,row=fixture();r=self.receiver(policy);row['l3']['directions'][0][field]=value
            with self.assertRaises(ValueError):r.accept(message(2,'snapshot',[row]))
            self.assertFalse(r.sessions)

    def test_pair_selected_by_the_producer_among_several_authorised(self):
        policy,row=fixture();policy['bindings']['wan2']=dict(device='wan2',index=3,alias='owner-wan2')
        row['rule']={'interface_pairs':[['lan','wan'],['lan','wan2']]}
        r=self.receiver(policy)
        with self.assertRaises(ValueError):r.accept(message(2,'snapshot',[row]))
        row['l3']['pair']=['lan','wan']
        r=self.receiver(policy);r.accept(message(2,'snapshot',[row]));self.assertEqual(len(r.sessions),1)
        row['l3']['pair']=['wan','lan']
        r=self.receiver(policy)
        with self.assertRaises(ValueError):r.accept(message(2,'snapshot',[row]))
        legacy_policy,legacy=fixture();r=self.receiver(legacy_policy);r.accept(message(2,'snapshot',[legacy]))
        self.assertEqual(len(r.sessions),1)

    def test_old_topology_cannot_enter_restarted_generation(self):
        policy,row=fixture();r=self.receiver(policy)
        r.accept(message(2,'snapshot',[row]));r.accept(message(3,'synchronized',{}))
        new=copy.deepcopy(PRODUCER);new['stream_id']='12345678-1234-4234-8234-123456789abc'
        policy['l3']['snapshot_digest']='e'*64
        r.accept(message(1,'begin',policy,producer=new))
        with self.assertRaises(ValueError):r.accept(message(2,'snapshot',[row],producer=new))
        self.assertFalse(r.sessions)

    def test_cp_must_acknowledge_the_same_topology_and_candidate_count(self):
        policy,row=fixture()
        for change in ({'snapshot_digest':'f'*64},{'candidates':99},{'observed':True}):
            frames=iter([message(1,'begin',policy)]);r=self.receiver(policy);reports=[]
            ack=protocol.acknowledgement(NONCE,r.status());ack['l3'].update(change)
            with self.assertRaises(ValueError):relay(NONCE,lambda:next(frames),lambda m:None,lambda:ack,reports.append)
            self.assertFalse(reports[-1]['ready']);self.assertFalse(reports[-1]['cp_acknowledged'])

    def test_legacy_observations_cannot_claim_l3_readiness(self):
        r=self.receiver(POLICY);r.accept(message(2,'snapshot',[ROW]));r.accept(message(3,'synchronized',{}))
        self.assertFalse(r.status()['l3_observed']);self.assertEqual(r.status()['l3_candidates'],0)
        _,row=fixture()
        with self.assertRaises(ValueError):r.accept(message(4,'upsert',row))


class L3ProducerTests(unittest.TestCase):
    def test_change_during_next_hop_calculation_prevents_publication(self):
        base=DPTests();base.setUp();out=[];changed=False
        def plan(*args):
            nonlocal changed
            changed=True
            return dict(available=False,hardware_admission=False,blockers=['no route'],directions=[],snapshot_digest='d'*64)
        def check(source,*_):
            if changed:raise dp.EventGap('neighbor changed while planning')
        with patch.object(dp.feed,'context',return_value=(base.state,base.collector,base.rules)), \
             patch.object(dp.feed.runtime,'saved',return_value=base.state), \
             patch.object(dp.feed.runtime,'status',return_value={}), \
             patch.object(dp.feed,'acknowledgement',return_value=base.collector), \
             patch.object(dp,'route_watch',return_value=contextlib.nullcontext(object())), \
             patch.object(dp,'check_routes',side_effect=check), \
             patch.object(dp,'subscribe',return_value=contextlib.nullcontext(object())), \
             patch.object(dp,'snapshot',return_value=([base.row],[])), \
             patch.object(dp.feed.l3,'snapshot',return_value={}), \
             patch.object(dp.feed.l3,'plan',side_effect=plan):
            with self.assertRaises(dp.EventGap):dp.stream(NONCE,out.append,stop=lambda:True)
        self.assertEqual([m['operation'] for m in out],['begin'])


if __name__=='__main__':unittest.main()
