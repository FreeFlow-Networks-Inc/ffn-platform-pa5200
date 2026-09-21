import unittest
from ffn_fe100_lifecycle import SessionLifecycle
from ffn_fe100_policy import PolicyOwner, digest
from ffn_fe100_sessions import SessionManager
from test_sessions import Backend

BOOT='11111111-1111-4111-8111-111111111111'


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.now=0;self.backend=Backend()
        self.bindings={'5':dict(next_hop=30,enabled=True,link=True),
                       '13':dict(next_hop=31,enabled=True,link=True)}
        self.owner=PolicyOwner(SessionManager(self.backend),lambda:None,lambda s:None,
                               lambda:self.bindings,lambda:True)
        self.owner.replace(0,'a'*64)
        self.life=SessionLifecycle(self.owner,heartbeat_timeout=10,idle_timeout=4,
                                  maximum_lifetime=8,clock=lambda:self.now)
        self.life.start(BOOT,1,'a'*64)
        self.request=dict(session_id=7,revision=1,policy_digest='a'*64,rule_id='rule-1',verdict='allow',
            protocol=17,src='198.18.0.1',dst='198.18.0.2',sport=40000,dport=40001,zone=4094,
            ingress=5,egress=13,inspection_required=False,nat_required=False,established=True)

    def open(self):self.life.event(BOOT,1,'open',self.request)

    def test_idle_expiry_removes_both_directions(self):
        self.open();self.assertEqual(len(self.backend.rows),2)
        self.now=4;self.life.tick()
        self.assertFalse(self.backend.rows);self.assertEqual(self.life.status()['leases'],0)

    def test_refresh_cannot_exceed_maximum_lifetime(self):
        self.open()
        for sequence,now in ((2,3),(3,6)):
            self.now=now;self.life.event(BOOT,sequence,'refresh',dict(session_id=7,decision_digest=digest(self.request)))
        self.now=8;self.life.tick();self.assertFalse(self.backend.rows)

    def test_stream_loss_drains_and_requires_new_start(self):
        self.open()
        with self.assertRaises(RuntimeError):self.life.event(BOOT,3,'heartbeat',{})
        self.assertFalse(self.backend.rows);self.assertFalse(self.life.status()['synchronized'])
        with self.assertRaises(RuntimeError):self.life.event(BOOT,2,'open',self.request)

    def test_stale_heartbeat_blocks_even_valid_later_events(self):
        self.open();self.now=10
        with self.assertRaises(RuntimeError):self.life.event(BOOT,2,'heartbeat',{})
        self.assertFalse(self.backend.rows)

    def test_link_and_routing_generation_change_invalidate(self):
        for key,value in (('link',False),('next_hop',32)):
            self.setUp();self.open();self.bindings['13'][key]=value;self.life.tick()
            self.assertFalse(self.backend.rows);self.assertFalse(self.life.status()['synchronized'])

    def test_explicit_close_and_policy_change(self):
        self.open();self.life.event(BOOT,2,'close',{'session_id':7});self.assertFalse(self.backend.rows)
        self.life.event(BOOT,3,'open',self.request);self.owner.replace(1,'b'*64);self.life.tick()
        self.assertFalse(self.life.status()['synchronized']);self.assertFalse(self.backend.rows)

    def test_failed_hardware_delete_retains_recovery_and_blocks(self):
        self.open();self.backend.delete=lambda key:None;self.now=4
        with self.assertRaises(RuntimeError):self.life.tick()
        self.assertFalse(self.owner.status()['admission_enabled'])
        self.assertTrue(self.owner.status()['recovery_required'])

    def test_untrusted_renewal_and_producer_restart(self):
        self.open()
        with self.assertRaises(ValueError):self.life.event(BOOT,2,'refresh',dict(session_id=7,decision_digest='b'*64))
        self.assertFalse(self.backend.rows)
        with self.assertRaises(ValueError):self.life.start(BOOT,0,'a'*64)
        self.assertFalse(self.owner.status()['admission_enabled'])
