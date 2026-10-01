import copy
import unittest
from types import SimpleNamespace
from fe100_attachment_config import compile_config
from test_fe100_attachment_config import XML
from ffn_fe100_attachment_runtime import resolve
from ffn_fe100_observations import Observations
from test_observations import NONCE,PRODUCER,POLICY,ROW,fragments,message


class Runtime(unittest.TestCase):
    def setUp(self):
        self.config=compile_config(XML);self.now=10
        self.receiver=SimpleNamespace(ready=True,tick=lambda:None,producer=PRODUCER,
            policy={'bindings':{'ethernet1/3':{'device':'p3','index':3,'alias':''},
                'ae4.82':{'device':'ae4.82','index':7,'alias':'ffn-aggregate:owner:ae4.82'}}})
        self.hardware=dict(epoch='epoch',physical={'3':dict(epoch='epoch',dp_boot_id=PRODUCER['boot_id'],enabled=True,pending=None)},
            groups={'ae4':dict(epoch='epoch',phase='active',ports=[9,17],heartbeat=10,token='owner',
                offload=True,trunk_created=True,egress=[9,17])})
    def status(self):return resolve(self.config,self.receiver,self.config['config_digest'],lambda:copy.deepcopy(self.hardware),lambda:self.now)
    def test_fresh_bindings_resolve_without_carrier_requirement(self):
        result=self.status();self.assertTrue(result['available']);self.assertFalse(result['hardware_admission'])
        self.assertTrue(all(r['ownership_observed'] for r in result['interfaces']))
        self.assertTrue(all(len(r['binding_revision'])==64 for r in result['interfaces']))
    def test_heartbeat_refresh_does_not_change_binding_generation(self):
        before=self.status()['interfaces'][0]['binding_revision']
        self.hardware['groups']['ae4']['heartbeat']=11;self.now=11
        self.assertEqual(self.status()['interfaces'][0]['binding_revision'],before)
    def test_expired_or_software_only_aggregate_never_qualifies_egress(self):
        self.now=25
        self.assertFalse(self.status()['interfaces'][0]['ownership_observed'])
        self.now=10;self.hardware['groups']['ae4']['offload']=False
        self.assertIn('Aggregate hardware egress selection is not commissioned',self.status()['interfaces'][0]['blockers'])
    def test_dp_restart_or_wrong_owner_is_not_adopted(self):
        self.hardware['physical']['3']['dp_boot_id']='old'
        self.assertFalse(self.status()['interfaces'][1]['ownership_observed'])
        self.receiver.policy['bindings']['ae4.82']['alias']='other'
        self.assertFalse(self.status()['interfaces'][0]['ownership_observed'])
    def test_missing_interface_or_config_mismatch_withholds_mapping(self):
        self.receiver.policy['bindings'].pop('ethernet1/3')
        self.assertFalse(self.status()['interfaces'][1]['ownership_observed'])
        self.assertFalse(resolve(self.config,self.receiver,'f'*64)['available'])
    def test_status_projection_stays_inside_control_rpc_budget(self):
        import json
        template=self.config['interfaces'][0]
        self.config['interfaces']=[template|dict(name='ae4.'+str(i),vlan=i) for i in range(1,101)]
        result=self.status()
        self.assertEqual(result['total'],100);self.assertTrue(result['truncated'])
        self.assertLess(len(json.dumps(result).encode()),32768)
    def test_configuration_never_replays_into_new_relay(self):
        owner=Observations(lambda:None,clock=lambda:1,configuration_digest=lambda:self.config['config_digest'])
        owner.start(NONCE)
        config=dict(schema=1,nonce=NONCE,configuration=self.config)
        for part in fragments(config):result=owner.chunk(part)
        self.assertEqual(result['ack']['config_digest'],self.config['config_digest'])
        self.assertFalse(owner.status()['ready'])
        for part in fragments(message(1,'begin',POLICY)):owner.chunk(part)
        self.assertEqual(owner.configuration,self.config)
        with self.assertRaises(ValueError):
            for part in fragments(config):owner.chunk(part)
        self.assertIsNone(owner.receiver)
        owner.start(NONCE);self.assertIsNone(owner.configuration)
    def test_wrong_committed_digest_is_rejected_before_snapshot(self):
        owner=Observations(lambda:None,clock=lambda:1,configuration_digest=lambda:'f'*64);owner.start(NONCE)
        with self.assertRaisesRegex(ValueError,'committed'):
            for part in fragments(dict(schema=1,nonce=NONCE,configuration=self.config)):owner.chunk(part)
        self.assertIsNone(owner.receiver)
    def test_cp_stream_acknowledges_intent_before_dp_snapshot(self):
        from ffn_fe100_session_stream import serve
        config=self.config;owners=[];out=[]
        class Bridge:
            def __init__(self,nonce):
                self.observations=Observations(lambda:None,clock=lambda:1,configuration_digest=lambda:config['config_digest'])
                self.observations.start(nonce);owners.append(self.observations)
            def send(self,value):
                for part in fragments(value):result=self.observations.chunk(part)
                return result['ack']
            def close(self):self.observations.close(NONCE)
        frames=iter([message(1,'begin',POLICY),message(2,'snapshot',[ROW]),message(3,'synchronized',{})])
        with self.assertRaises(StopIteration):
            serve(NONCE,lambda:next(frames),out.append,lambda r:None,Bridge,config)
        self.assertEqual(out[0]['config_digest'],config['config_digest'])
        self.assertEqual([v['sequence'] for v in out[1:]],[1,2,3])
        self.assertFalse(owners[0].status()['ready'])


if __name__=='__main__':unittest.main()
