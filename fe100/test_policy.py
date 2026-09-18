import unittest
from ffn_fe100_policy import PolicyOwner
from ffn_fe100_sessions import SessionManager
from test_sessions import Backend


class Policy(unittest.TestCase):
    def setUp(self):
        self.backend=Backend();self.manager=SessionManager(self.backend)
        self.saved=[];self.binding={'5':{'next_hop':30,'enabled':True,'link':True},
                                   '13':{'next_hop':31,'enabled':True,'link':True}}
        self.ready=True
        self.owner=PolicyOwner(self.manager,lambda:None,lambda s:self.saved.append(dict(s)),
                               lambda:self.binding,lambda:self.ready)
        self.owner.replace(0,'a'*64);self.owner.activate(1,'a'*64)
        self.request=dict(session_id=7,revision=1,policy_digest='a'*64,rule_id='rule-1',verdict='allow',
            protocol=17,src='198.18.0.1',dst='198.18.0.2',sport=40000,dport=40001,zone=4094,
            ingress=5,egress=13,inspection_required=False,nat_required=False,established=True)

    def test_admit_pair_and_invalidate_before_generation_advances(self):
        self.owner.admit(self.request)
        self.assertEqual(len(self.backend.rows),2)
        self.owner.replace(1,'b'*64)
        self.assertFalse(self.backend.rows)
        self.assertEqual(self.owner.state['phase'],'blocked')
        self.assertTrue(any(s['phase']=='draining' for s in self.saved))
        with self.assertRaises(RuntimeError):self.owner.admit(self.request)

    def test_unsupported_or_stale_decisions_never_write(self):
        for key,value in (('revision',0),('policy_digest','b'*64),('verdict','deny'),
                          ('nat_required',True),('inspection_required',True),('established',False),
                          ('egress',5),('ingress',24)):
            with self.assertRaises((RuntimeError,ValueError),msg=key):self.owner.admit(self.request | {key:value})
        self.assertEqual(self.backend.writes,0)

    def test_attachment_change_drains_both_directions(self):
        self.owner.admit(self.request);self.binding['13']['link']=False
        with self.assertRaises(RuntimeError):self.owner.admit(self.request | {'session_id':8})
        self.assertFalse(self.backend.rows);self.assertEqual(self.owner.state['phase'],'blocked')

    def test_failed_delete_keeps_policy_blocked(self):
        self.owner.admit(self.request);self.backend.delete=lambda k:None
        with self.assertRaises(RuntimeError):self.owner.replace(1,'b'*64)
        self.assertEqual(self.owner.state['phase'],'draining')
        with self.assertRaises(RuntimeError):self.owner.activate(2,'b'*64)

    def test_second_direction_failure_rolls_back(self):
        self.backend.fail=2
        with self.assertRaises(TimeoutError):self.owner.admit(self.request)
        self.assertFalse(self.backend.rows)

    def test_loss_of_qualification_drains(self):
        self.owner.admit(self.request);self.ready=False
        with self.assertRaises(RuntimeError):self.owner.admit(self.request | {'session_id':8})
        self.assertFalse(self.backend.rows)

    def test_revoke_and_persistence_failure(self):
        self.owner.admit(self.request);self.owner.revoke(7)
        self.assertFalse(self.backend.rows)
        def fail(s):raise OSError('disk full')
        self.owner.save=fail
        with self.assertRaises(OSError):self.owner.replace(1,'b'*64)
        with self.assertRaises(RuntimeError):self.owner.admit(self.request)

    def test_uninitialized_owner_cannot_activate(self):
        owner=PolicyOwner(self.manager,lambda:None,lambda s:None,lambda:self.binding,lambda:True)
        with self.assertRaises(ValueError):owner.activate(0,None)
        self.assertFalse(owner.status()['admission_enabled'])


if __name__=='__main__':unittest.main()
