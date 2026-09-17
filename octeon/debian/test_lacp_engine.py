import copy
import unittest
from ffn_lacp_engine import Engine,encode,ZERO,SYNC,COLLECTING,DISTRIBUTING,ACTIVITY,AGGREGATION,TIMEOUT
from ffn_lacp_packets import decode

class Gates:
    def __init__(self):self.history=[];self.fail=False
    def apply(self,value):
        self.history.append(copy.deepcopy(value))
        if self.fail:raise RuntimeError('gate failed')
        return value

def engine(system='02:00:00:00:00:01',**kw):
    return Engine(system,1,{1:system,2:system},Gates(),**kw)

def run(a,b,end=10,start=0,drop=None):
    for n in range(int(start*10),int(end*10)):
        now=n/10
        for e in (a,b):
            for port in e.members:e.link(port,True,10000,now)
        for source,target in ((a,b),(b,a)):
            for port,frame in source.transmissions(now):
                if not drop or not drop(source,port,now):target.receive(port,frame,now)
    return now

class NegotiationTests(unittest.TestCase):
    def test_two_active_peers_negotiate_two_acknowledged_members(self):
        a=engine();b=engine('02:00:00:00:00:02');now=run(a,b)
        self.assertEqual(a.status(now)['distributing'],[1,2]);self.assertEqual(b.status(now)['distributing'],[1,2])
        self.assertEqual(a.driver.history[0],a.closed())
        for port,frame in a.transmissions(now+1):
            self.assertEqual(decode(frame)['actor']['state'] & (SYNC|COLLECTING|DISTRIBUTING),SYNC|COLLECTING|DISTRIBUTING)

    def test_active_passive_works_but_two_passive_do_not_initiate(self):
        for aa,bb,expected in [('active','passive',[1,2]),('passive','active',[1,2]),('passive','passive',[])]:
            a=engine(activity=aa);b=engine('02:00:00:00:00:02',activity=bb);now=run(a,b)
            self.assertEqual(a.status(now)['distributing'],expected)
            if not expected:self.assertEqual(sum(m['tx'] for m in a.members.values()),0)

    def test_receive_timeout_withdraws_before_defaulting_and_recovers(self):
        a=engine();b=engine('02:00:00:00:00:02');run(a,b)
        now=run(a,b,14,10,lambda src,p,t:src is b and p==1)
        self.assertEqual(a.status(now)['distributing'],[2]);self.assertEqual(a.members[1]['rx'],'expired')
        now=run(a,b,18,14,lambda src,p,t:src is b and p==1)
        self.assertEqual(a.members[1]['rx'],'defaulted')
        now=run(a,b,26,18);self.assertEqual(a.status(now)['distributing'],[1,2])

    def test_carrier_down_speed_change_and_expired_lease_withdraw(self):
        for change in ('down','speed','lease'):
            a=engine();b=engine('02:00:00:00:00:02');now=run(a,b)
            if change=='down':a.link(1,False,0,now)
            elif change=='speed':a.link(1,True,1000,now)
            else:a.tick(now+3)
            self.assertNotIn(1,a.status(a.now)['distributing'])
            if change=='lease':self.assertEqual(a.status(a.now)['distributing'],[])

    def test_minimum_links_and_stop(self):
        a=engine(min_links=2);b=engine('02:00:00:00:00:02');now=run(a,b)
        self.assertEqual(a.status(now)['distributing'],[1,2])
        a.link(1,False,0,now);self.assertEqual(a.status(now)['distributing'],[])
        a.stop(now);self.assertEqual(a.applied,a.closed())

    def test_malformed_loopback_and_mismatched_echo_cannot_enable(self):
        a=engine();a.link(1,True,10000,0)
        self.assertFalse(a.receive(1,b'bad',0))
        self.assertFalse(a.receive(1,encode(a.actor(1),ZERO,a.system),0))
        peer=dict(a.actor(1),system='02:00:00:00:00:02',state=63)
        for n in range(50):
            t=n/10;a.link(1,True,10000,t);a.receive(1,encode(peer,ZERO,peer['system']),t)
        self.assertEqual(a.status(4.9)['distributing'],[])
        self.assertFalse(a.members[1]['matched'])

    def test_partner_system_and_key_separate_aggregators_duplicate_port_rejected(self):
        a=engine();b=engine('02:00:00:00:00:02');now=run(a,b)
        for change in ({'system':'02:00:00:00:00:03'},{'key':9},{'port':2}):
            peer=dict(b.actor(1),**change)
            a.receive(1,encode(peer,a.actor(1),b.system),now)
            self.assertNotIn(1,a.status(now)['distributing'])
        self.assertEqual(a.status(now)['distributing'],[],'Duplicate peer port invalidates both member identities')

    def test_driver_failure_latches_and_never_advertises_collect_distribute(self):
        a=engine();b=engine('02:00:00:00:00:02');a.driver.fail=True;now=run(a,b)
        self.assertIsNotNone(a.fault);self.assertEqual(a.status(now)['distributing'],[])
        self.assertEqual(a.actor(1)['state']&(COLLECTING|DISTRIBUTING),0)
        a.driver.fail=False;count=len(a.driver.history);run(a,b,12,10)
        self.assertEqual(len(a.driver.history),count,'Fault requires explicit owner recovery')
        a.stop(12);self.assertEqual(a.applied,a.closed());self.assertIsNotNone(a.fault)

    def test_exception_without_message_still_latches_fault(self):
        class Bad:
            def apply(self,value):raise RuntimeError()
        a=Engine('02:00:00:00:00:01',1,{1:'02:00:00:00:00:01'},Bad())
        self.assertEqual(a.fault,'RuntimeError');a.link(1,True,10000,0)
        self.assertEqual(a.transmissions(1),[])

    def test_periodic_transmissions_obey_peer_timeout_and_rate_limit(self):
        a=engine();b=engine('02:00:00:00:00:02',rate='slow');now=run(a,b)
        before=a.members[1]['tx'];run(a,b,20,10)
        self.assertLessEqual(a.members[1]['tx']-before,1,'Slow peer requests a 30-second transmit period')
        a.members[1]['dirty']=True;a.transmissions(20)
        a.members[1]['dirty']=True;self.assertEqual(a.transmissions(20.1),[])

    def test_clock_rollback_invalid_identity_and_readback(self):
        a=engine();a.tick(5)
        with self.assertRaises(ValueError):a.tick(4)
        with self.assertRaises(ValueError):engine('01:00:00:00:00:01')
        with self.assertRaises(ValueError):Engine('02:00:00:00:00:01',True,{1:'02:00:00:00:00:01'},Gates())
        class Bad:
            def apply(self,value):return {}
        a=Engine('02:00:00:00:00:01',1,{1:'02:00:00:00:00:01'},Bad())
        self.assertIn('readback',a.fault);self.assertIsNone(a.applied)

if __name__=='__main__':unittest.main()
