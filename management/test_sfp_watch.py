import json
import unittest
from unittest.mock import patch
import ffn_faceplate as face
import ffn_sfp_watch as watch


def port_row(**changes):
    row=dict(port=5,name='ethernet1/5',bcm_port=16,media='sfp',available=True,mac_enabled=True,speed_configuration=True,
             configured_speed='1000',link=False,link_mode='SGMII',module=None)
    row.update(changes);return row


class RelinkTests(unittest.TestCase):
    def state(self,row,pending=None):
        ports=[dict(port=p,media='copper',available=False) for p in range(1,25)];ports[4]=row
        return {'ports':ports,'saved':{'pending':pending} if pending else {'ports':{}},'revision':1}

    def test_relink_reapplies_the_configured_speed_with_the_module_recipe(self):
        rows=[port_row(),port_row(link=True,link_mode='1000BASE-X',module=dict(part='GLC-LH-SMD'))]
        calls=[]
        def observe():return self.state(rows.pop(0))
        def link_apply(row,speed):calls.append(('recipe',row['port'],speed));return dict(speed=1000,autoneg=True,link_mode='1000BASE-X')
        result=face.relink(5,observe=observe,link_apply=link_apply,link_set=lambda r:calls.append(('generic',r)),member=lambda p:False)
        self.assertEqual(calls,[('recipe',5,'1000')])
        self.assertEqual((result['result'],result['speed'],result['link'],result['link_mode'],result['mode']['autoneg']),('applied','1000',True,'1000BASE-X',True))

    def test_unclassified_module_uses_the_generic_sdk_path(self):
        rows=[port_row(configured_speed='auto'),port_row(configured_speed='auto')];calls=[]
        result=face.relink(5,observe=lambda:self.state(rows.pop(0)),link_apply=lambda row,speed:False,
                           link_set=lambda r:calls.append(r),member=lambda p:False)
        self.assertEqual(calls,[{'op':'port.link.set','port':16,'speed':'auto'}]);self.assertEqual(result['mode'],'generic')

    def test_guards_leave_the_port_alone(self):
        cases=[(port_row(),'pending-operation',{'port':5,'speed':'1000'},False),
               (port_row(media='copper'),'not-an-sfp-port',None,False),
               (port_row(available=False),'not-an-sfp-port',None,False),
               (port_row(),'aggregate-member',None,True),
               (port_row(mac_enabled=False),'unconfigured',None,False),
               (port_row(configured_speed=None),'unconfigured',None,False),
               (port_row(speed_configuration=False),'unconfigured',None,False)]
        for row,expected,pending,member in cases:
            with self.subTest(expected=expected):
                result=face.relink(5,observe=lambda:self.state(row,pending),link_apply=lambda *a:self.fail('applied'),
                                   link_set=lambda r:self.fail('generic'),member=lambda p:member)
                self.assertEqual(result['result'],expected)


class ReadbackTests(unittest.TestCase):
    def test_expected_readbacks_follow_the_module_mode(self):
        self.assertEqual(face.expected_speeds({'speed':'auto'},False),{'auto'})
        self.assertEqual(face.expected_speeds({'speed':'1000'},dict(speed=1000,autoneg=True)),{'1000','auto'})
        self.assertEqual(face.expected_speeds({'speed':'auto'},dict(speed=10000,autoneg=False)),{'10000'})


class FakeCages:
    def __init__(self):self.present=set();self.readable=set()
    def inventory(self):return {p:dict(present=p in self.present) for p in range(5,21)}
    def identity(self,port):
        if port not in self.readable:raise ValueError('Invalid SFP identity or checksum')
        return b'identity'


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.cages=FakeCages();self.now=100.0;self.events=[];self.relinks=[]
        self.watcher=watch.Watcher(self.cages.inventory,self.cages.identity,self.relink,clock=lambda:self.now,
                                   log=lambda line:self.events.append(json.loads(line)))

    def relink(self,port):
        self.relinks.append(port);return dict(port=port,result='applied',speed='1000')

    def advance(self,seconds=2.0):
        self.now+=seconds;self.watcher.poll()

    def test_modules_present_at_start_are_not_touched_and_insertion_relinks_once(self):
        self.cages.present={5,9};self.cages.readable={5,9}
        self.watcher.poll();self.advance();self.advance()
        self.assertEqual(self.relinks,[])
        self.cages.present.add(7);self.cages.readable.add(7)
        self.advance()  # inserted: identity must settle first
        self.assertEqual(self.relinks,[]);self.assertEqual(self.events[-1]['event'],'inserted')
        self.advance()
        self.assertEqual(self.relinks,[7]);self.assertEqual(self.events[-1],dict(event='relink',port=7,result='applied',speed='1000'))
        self.advance();self.advance()
        self.assertEqual(self.relinks,[7])

    def test_unreadable_identity_waits_then_gives_up_and_removal_cancels(self):
        self.watcher.poll()
        self.cages.present.add(6)
        for _ in range(5):self.advance()
        self.assertEqual(self.relinks,[]);self.assertEqual([e['event'] for e in self.events],['inserted'])
        self.cages.readable.add(6);self.advance()
        self.assertEqual(self.relinks,[6])
        self.cages.present.add(8);self.advance();self.advance()
        self.cages.present.discard(8);self.advance()
        self.assertEqual(self.events[-1],dict(event='removed-before-relink',port=8));self.assertEqual(self.relinks,[6])
        self.cages.present.add(10);self.advance()
        self.now+=watch.IDENTITY_DEADLINE;self.advance()
        self.assertEqual(self.events[-1]['event'],'identity-unreadable');self.assertEqual(self.relinks,[6])
        self.assertNotIn(10,self.watcher.pending)

    def test_relink_errors_and_presence_failures_are_journaled_not_raised(self):
        self.watcher.poll();self.cages.present.add(5);self.cages.readable.add(5)
        with patch.object(self.watcher,'relink',side_effect=RuntimeError('BCM operation failed')):
            self.advance();self.advance()
        self.assertEqual(self.events[-1],dict(event='relink',port=5,result='error',error='BCM operation failed'))
        good=self.cages.inventory
        self.cages.inventory=lambda:(_ for _ in ()).throw(OSError('bus'))
        self.watcher.inventory=self.cages.inventory;self.advance()
        self.assertEqual(self.events[-1]['event'],'presence-unavailable');self.assertEqual(self.events[-1]['backoff_seconds'],watch.BACKOFF)
        # A wedged bus is left alone for the back-off; polling resumes afterwards.
        count=len(self.events);self.watcher.inventory=good;self.advance();self.advance()
        self.assertEqual(len(self.events),count)
        self.now+=watch.BACKOFF;self.advance()
        self.assertEqual(len(self.events),count);self.assertIsNotNone(self.watcher.previous)


class VerifyTests(unittest.TestCase):
    """The transmit check runs on its own cadence, journals verdict changes and applies local remedies once."""
    def setUp(self):
        self.now=1000.0;self.events=[];self.checks=[];self.remedied=[]
        self.present={5:True,13:False}
        self.results=[dict(port=5,verdict='ok',detail='',remedy=None,diagnostics=dict(tx_power_dbm=-5.2,rx_power_dbm=-10.2))]
        def check(present):
            self.checks.append(dict(present))
            if isinstance(self.results,Exception):raise self.results
            return [dict(r) for r in self.results]
        self.watcher=watch.Watcher(lambda:{p:dict(present=v) for p,v in self.present.items()},lambda port:b'x',lambda port:dict(port=port,result='relinked'),
                                   clock=lambda:self.now,log=lambda line:self.events.append(json.loads(line)),check=check,
                                   remedies={'enable-transmitter':lambda r:self.remedied.append(('tx',r['port'])) or dict(result='transmitter-enabled'),
                                             'relink':lambda r:self.remedied.append(('relink',r['port'])) or dict(result='relinked')},
                                   check_interval=60)
    def test_check_runs_at_its_interval_and_journals_only_verdict_changes(self):
        self.watcher.poll();self.assertEqual(len(self.checks),1)
        self.assertEqual([e['event'] for e in self.events],['transmit']);self.assertEqual(self.events[0]['verdict'],'ok');self.assertEqual(self.events[0]['tx_power_dbm'],-5.2)
        self.now+=5;self.watcher.poll();self.assertEqual(len(self.checks),1)          # not yet
        self.now+=60;self.watcher.poll();self.assertEqual(len(self.checks),2)         # same verdict: no new event
        self.assertEqual(len(self.events),1)
        self.results=[dict(port=5,verdict='no-rx-light',detail='no light from the far end (RX -35 dBm)',remedy='far-end',diagnostics=dict(tx_power_dbm=-5.2,rx_power_dbm=-35.0))]
        self.now+=60;self.watcher.poll()
        self.assertEqual(self.events[-1]['event'],'transmit');self.assertEqual(self.events[-1]['verdict'],'no-rx-light');self.assertEqual(self.remedied,[])
    def test_local_remedies_run_once_per_verdict_and_the_port_is_rejudged(self):
        self.results=[dict(port=5,verdict='transmitter-off',detail='TX_DISABLE asserted by the cage control line',remedy='enable-transmitter',diagnostics=None)]
        self.watcher.poll()
        self.assertEqual(self.remedied,[('tx',5)]);self.assertEqual([e['event'] for e in self.events],['transmit','remediated'])
        self.assertEqual(self.events[-1]['result'],'transmitter-enabled')
        self.now+=60;self.watcher.poll()                    # still off: judged again and remedied again, since it was re-judged
        self.assertEqual(self.remedied,[('tx',5),('tx',5)])
        self.results=[dict(port=5,verdict='link-mode-mismatch',detail='switch runs SGMII, module needs GMII',remedy='relink',diagnostics=None)]
        self.now+=60;self.watcher.poll();self.assertEqual(self.remedied[-1],('relink',5))
        self.results=[dict(port=5,verdict='attachment-pending',detail='left pending',remedy='reapply',diagnostics=None)]
        self.now+=60;self.watcher.poll();self.assertEqual(self.remedied[-1],('relink',5))   # no local remedy for reapply
        self.assertEqual(self.events[-1]['remedy'],'reapply')
    def test_a_failing_check_backs_off_like_a_failing_bus_and_insertion_work_takes_precedence(self):
        self.results=OSError('[Errno 145] Connection timed out')
        self.watcher.poll()
        self.assertEqual(self.events[-1]['event'],'check-unavailable');self.assertEqual(self.watcher.quiet_until,self.now+watch.BACKOFF)
        self.results=[dict(port=5,verdict='ok',detail='',remedy=None,diagnostics=None)]
        self.now+=watch.BACKOFF+1;self.present[13]=True;self.watcher.poll()
        self.assertIn(13,self.watcher.pending);self.assertEqual(len(self.checks),1)       # a pending insertion defers the check
        self.now+=watch.SETTLE+1;self.watcher.poll();self.assertNotIn(13,self.watcher.pending)
        self.assertEqual(len(self.checks),2)
    def test_no_module_and_disabled_verdicts_are_not_journaled(self):
        self.results=[dict(port=5,verdict='no-module',detail='no transceiver in the cage',remedy=None,diagnostics=None),dict(port=13,verdict='disabled',detail='',remedy=None,diagnostics=None)]
        self.watcher.poll();self.assertEqual(self.events,[])


if __name__=='__main__':unittest.main()
