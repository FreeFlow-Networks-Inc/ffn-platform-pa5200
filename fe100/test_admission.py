import copy
import json
import unittest
from types import SimpleNamespace
import ffn_fe100_admission as A

ORIGINAL=dict(source='192.0.2.2',destination='198.51.100.2',source_port=12000,destination_port=443,protocol=6)
REPLY=dict(source='198.51.100.2',destination='192.0.2.2',source_port=443,destination_port=12000,protocol=6)
TRANSLATED=dict(source=REPLY['destination'],destination=REPLY['source'],source_port=REPLY['destination_port'],
                destination_port=REPLY['source_port'],protocol=6)
POLICY=dict(revision=7,digest='a'*64,nat_digest='b'*64,bindings={
    'ethernet1/1':dict(device='p1',index=1,alias=''),'ethernet1/5':dict(device='p5',index=5,alias='')})
OWNER=dict(phase='blocked',revision=7,digest='a'*64,admission_enabled=False)
CAPABILITIES=dict(nat_packet_qualification=False)


def direction(name,index,destination,vlan=None):
    return dict(device=name,index=index,alias='',interface=name,destination=destination,next_hop=destination,
                mtu=1500,vlan=vlan,decrement_ttl=True,source_mac='02:00:00:00:00:01',
                destination_mac='02:00:00:00:00:02',exceptions=['ttl-expired','mtu-exceeded','ipv4-fragments'])


def row(identity='c'*64,**changes):
    value=dict(identity=identity,conntrack_id=8,token='t',
               rule=dict(scope='vsys1',name='allow-out',interface_pairs=[['ethernet1/5','ethernet1/1']],inspection_required=False),
               original=ORIGINAL,reply=REPLY,translated=TRANSLATED,nat=dict(source=False,destination=False),
               counters={},remaining_seconds=60,tcp_state=3,software_candidate=True,blockers=[],
               l3=dict(available=True,hardware_admission=False,blockers=[],snapshot_digest='d'*64,
                       directions=[direction('ethernet1/1',1,'198.51.100.2'),direction('ethernet1/5',5,'192.0.2.2')]))
    value.update(changes);return value


def attachment(name,observed=True,blockers=()):
    return dict(intent=dict(name=name),ownership_observed=observed,binding_revision='e'*64 if observed else None,
                blockers=list(blockers),remaining=['Commissioned FE100 zone and miss path','LIF/LEF and QMAP allocation'])


def attachments(*rows,truncated=False):
    return dict(available=True,hardware_admission=False,interfaces=list(rows),truncated=truncated)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.receiver=SimpleNamespace(ready=True,policy=copy.deepcopy(POLICY),sessions={'c'*64:row()})
        self.attachments=attachments(attachment('ethernet1/1'),attachment('ethernet1/5'))

    def run_eval(self,**kw):
        kw.setdefault('configuration_digest',OWNER['digest'])
        return A.evaluate(self.receiver,self.attachments,OWNER,CAPABILITIES,**kw)

    def test_clean_session_is_admissible_only_pending_generation_and_commissioning(self):
        result=self.run_eval()
        self.assertEqual((result['mode'],result['hardware_admission'],result['installed']),(A.MODE,False,0))
        item=result['sessions'][0]
        self.assertEqual(item['stage'],'admissible-pending-commissioning');self.assertEqual(item['blockers'],[])
        self.assertEqual(item['interfaces'],['ethernet1/5','ethernet1/1']);self.assertFalse(item['nat']);self.assertTrue(item['established'])
        self.assertEqual(result['admissible_pending_commissioning'],1)
        self.assertIn('policy generation is not activated for hardware admission (phase blocked)',result['generation'])
        self.assertIn('front-port offload is not qualified',result['generation'])
        self.assertIn('durable hardware flow-ID allocator is not commissioned',result['generation'])
        self.assertIn('Commissioned FE100 zone and miss path',result['commissioning'])
        self.assertIn('egress LIF per attachment',result['commissioning'])
        self.assertEqual(result['reasons'],{})

    def test_generation_reasons_compare_the_relayed_commit_barrier_not_the_dp_generation(self):
        active=dict(OWNER,phase='active',admission_enabled=True)
        self.assertEqual(A.generation(active,True,True,'f'*64),['relayed configuration intent differs from the CP policy barrier'])
        self.assertEqual(A.generation(active,True,True,None),['committed interface intent has not been relayed for this generation'])
        self.assertEqual(A.generation(active,True,True,OWNER['digest']),[])
        # The DP's own policy generation (revision 9 / its plan digest) never has to equal the owner's barrier.
        self.receiver.policy.update(revision=9,digest='9'*64)
        result=self.run_eval()
        self.assertNotIn('relayed configuration intent differs from the CP policy barrier',result['generation'])
        self.assertEqual(result['sessions'][0]['stage'],'admissible-pending-commissioning')

    def test_software_candidate_blockers_pass_through_without_evaluation(self):
        self.receiver.sessions['c'*64]=row(software_candidate=False,blockers=['tcp-not-established'],original={},reply={},l3=None)
        item=self.run_eval()['sessions'][0]
        self.assertEqual((item['stage'],item['blockers']),('software',['tcp-not-established']))

    def test_each_production_gate_names_its_own_reason(self):
        cases=[
            (dict(rule=dict(scope='vsys1',name='r',interface_pairs=[['ethernet1/5','ethernet1/1'],['ethernet1/1','ethernet1/5']],inspection_required=False)),'interface pair is ambiguous'),
            (dict(rule=dict(scope='vsys1',name='r',interface_pairs=[['ethernet1/5','ethernet1/1']],inspection_required=True)),'flow requires software enforcement: inspection profile'),
            (dict(tcp_state=2),'flow requires software enforcement: not established'),
            (dict(nat=dict(source=True,destination=False)),'NAT packet rewrite is not qualified for hardware admission'),
            (dict(conntrack_id=2**31),'session tuple encoding: '),
            (dict(l3=None),'DP route/neighbor observation is unavailable'),
            (dict(l3=dict(available=False,hardware_admission=False,blockers=['No route to 198.51.100.2'],directions=[],snapshot_digest='d'*64)),'No route to 198.51.100.2'),
            (dict(rule=dict(scope='vsys1',name='r',interface_pairs=[['ethernet1/5','ethernet1/9']],inspection_required=False)),'interface owner is absent from applied bindings: ethernet1/9'),
        ]
        for changes,expected in cases:
            self.receiver.sessions['c'*64]=row(**changes)
            item=self.run_eval()['sessions'][0]
            with self.subTest(expected=expected):
                self.assertEqual(item['stage'],'blocked')
                self.assertTrue(any(reason.startswith(expected) for reason in item['blockers']),item['blockers'])

    def test_attachment_ownership_and_intent_gaps_are_per_interface(self):
        self.attachments=attachments(attachment('ethernet1/1',observed=False,blockers=['CP physical redirect ownership is missing, stale or pending']))
        item=self.run_eval()['sessions'][0]
        self.assertIn('ethernet1/1: CP physical redirect ownership is missing, stale or pending',item['blockers'])
        self.assertIn('no committed Layer 3 attachment intent: ethernet1/5',item['blockers'])
        self.attachments=attachments(attachment('ethernet1/1'),truncated=True)
        self.assertIn('attachment intent projection is truncated: ethernet1/5',self.run_eval()['sessions'][0]['blockers'])

    def test_directional_plan_is_validated_with_the_path_owner_shape(self):
        for field,value,expected in [('destination','192.0.2.99','does not match the translated destination'),
                                     ('index',99,'owner differs from applied binding'),
                                     ('destination_mac','ff:ff:ff:ff:ff:ff','destination MAC'),
                                     ('mtu',1,'invalid MTU'),('vlan',5000,'invalid VLAN'),
                                     ('source_mac','01:00:00:00:00:01','source MAC')]:
            value_row=row();value_row['l3']['directions'][0][field]=value
            self.receiver.sessions['c'*64]=value_row
            item=self.run_eval()['sessions'][0]
            with self.subTest(field=field):
                self.assertEqual(len(item['blockers']),1);self.assertTrue(item['blockers'][0].startswith('directional path plan: '))
                self.assertIn(expected,item['blockers'][0])
        tagged=row();tagged['l3']['directions'][1]['vlan']=80
        self.receiver.sessions['c'*64]=tagged
        self.assertEqual(self.run_eval()['sessions'][0]['blockers'],[])

    def test_request_mirrors_the_paired_admission_fields(self):
        request=A.request_for(row(),dict(POLICY,revision=9,digest='9'*64),OWNER,{'ethernet1/5':5,'ethernet1/1':1},('ethernet1/5','ethernet1/1'))
        self.assertEqual((request['revision'],request['policy_digest'],request['nat_digest']),(OWNER['revision'],OWNER['digest'],POLICY['nat_digest']))
        self.assertEqual(set(request),{'session_id','revision','policy_digest','nat_digest','rule_id','verdict','original',
                                       'reply','ingress','egress','inspection_required','nat_required','established'})
        self.assertEqual((request['rule_id'],request['ingress'],request['egress'],request['verdict']),('vsys1/allow-out',5,1,'allow'))
        self.assertEqual(A.rule_id({},'token-only'),'token-only');self.assertIsNone(A.rule_id({},'x'*129))

    def test_unready_inventory_bounded_work_and_projection(self):
        for receiver in (None,SimpleNamespace(ready=False,policy=POLICY,sessions={}),SimpleNamespace(ready=True,policy=None,sessions={})):
            result=A.evaluate(receiver,self.attachments,OWNER,CAPABILITIES)
            self.assertFalse(result['available']);self.assertFalse(result['hardware_admission']);self.assertEqual(result['evaluated'],0)
        self.receiver.sessions={('%064x'%i):row(identity='%064x'%i,conntrack_id=i) for i in range(1,301)}
        result=self.run_eval(bound=100,limit=4096)
        self.assertEqual((result['evaluated'],result['total'],result['bounded']),(100,300,True))
        self.assertTrue(result['truncated']);self.assertLess(len(json.dumps(result['sessions']).encode()),4096+1024)
        self.assertEqual(result['stages']['admissible-pending-commissioning'],100)
        self.assertEqual([s['session_id'] for s in result['sessions']],sorted(s['session_id'] for s in result['sessions']))
        full=self.run_eval()
        self.assertEqual((full['evaluated'],full['bounded']),(A.MAX_EVALUATED,True))
        self.assertLess(len(json.dumps(full).encode()),A.PROJECTION_BYTES+4096)


if __name__=='__main__':unittest.main()
