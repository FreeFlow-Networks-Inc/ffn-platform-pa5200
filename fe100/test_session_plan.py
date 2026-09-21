import copy
import unittest
import uuid
from ffn_fe100_session_plan import plan,reverse


class SessionPlanTests(unittest.TestCase):
    def setUp(self):
        original=dict(source='192.0.2.2',destination='198.51.100.2',source_port=12000,destination_port=443,protocol=6)
        reply=dict(source='198.51.100.2',destination='203.0.113.2',source_port=443,destination_port=14000,protocol=6)
        nonce=str(uuid.uuid4())
        self.request=dict(nonce=nonce,observation=dict(schema=1,nonce=nonce,available=True,hardware_admission=False,
            source='kernel-conntrack-with-durable-security-grants',observed_monotonic=10,completed_monotonic=11,
            producer=dict(boot_id=str(uuid.uuid4()),pid=4,process_start='42',reconciliations=1),
            policy=dict(revision=2,digest='a'*64,nat_digest='b'*64,bindings={
                'ae2.80':dict(device='ae2.80',index=10,alias='current-owner'),
                'ethernet1/2':dict(device='p2',index=11,alias='')}),
            owned_sessions=1,truncated=False,sessions=[dict(identity='c'*64,conntrack_id=8,blockers=[],
                software_candidate=True,remaining_seconds=60,counters={},rule={'interface_pairs':[['ae2.80','ethernet1/2']]},
                original=original,reply=reply,translated=reverse(reply))]))

    def test_actual_two_way_nat_and_current_owners_without_allocated_hardware_ids(self):
        result=plan(self.request,{'initialized':False,'blockers':['calibration missing']})
        self.assertFalse(result['hardware_admission']);self.assertFalse(result['hardware_initialized'])
        row=result['sessions'][0]
        self.assertTrue(row['nat']);self.assertFalse(row['hardware_eligible'])
        self.assertEqual(row['directions'][0]['translated']['source_port'],14000)
        self.assertEqual(row['directions'][1]['translated']['destination'],'192.0.2.2')
        self.assertEqual(row['interfaces']['ae2.80']['index'],10)
        self.assertNotIn('next_hops',row);self.assertNotIn('zone_id',row)
        self.assertIn('calibration missing',row['blockers'])

    def test_stale_mismatched_malformed_and_duplicate_observations_rejected(self):
        for field,value in [('nonce',str(uuid.uuid4())),('hardware_admission',True),('completed_monotonic',20),
                            ('completed_monotonic',float('nan')),('available',False),('truncated',True)]:
            request=copy.deepcopy(self.request);request['observation'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):plan(request,{})
        for change in ({'translated':{}},{'software_candidate':False}):
            request=copy.deepcopy(self.request);request['observation']['sessions'][0].update(change)
            with self.assertRaises(ValueError):plan(request,{})
        request=copy.deepcopy(self.request);request['observation']['sessions']*=2;request['observation']['owned_sessions']=2
        with self.assertRaises(ValueError):plan(request,{})

    def test_unknown_binding_and_non_candidate_are_explicitly_blocked(self):
        self.request['observation']['policy']['bindings'].clear()
        row=plan(self.request,{})['sessions'][0]
        self.assertTrue(any('interface owner' in r for r in row['blockers']))
        original=self.request['observation']['sessions'][0]
        original.update(software_candidate=False,blockers=['unsupported-protocol'],original={},reply={})
        row=plan(self.request,{})['sessions'][0]
        self.assertNotIn('directions',row);self.assertIn('unsupported-protocol',row['blockers'])


if __name__=='__main__':unittest.main()
