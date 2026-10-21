import socket
import unittest
from unittest.mock import patch
import ffn_session_stream_dp as producer
from ffn_session_events import EventGap


class Watch:
    """A route-watch stand-in: readable while it holds notifications."""
    def __init__(self):
        self.pair=socket.socketpair();self.pair[1].setblocking(False)
    def notify(self,count=1):
        for _ in range(count):self.pair[0].send(b'x')
    def fileno(self):return self.pair[1].fileno()
    def recv(self,size):return self.pair[1].recv(size)
    def close(self):
        for s in self.pair:s.close()


class RouteCheckTests(unittest.TestCase):
    def setUp(self):
        self.watch=Watch();self.addCleanup(self.watch.close)
        self.topology={'neighbors':[dict(dst='203.0.113.1',dev='p7',lladdr='02:00:00:00:00:07',valid=True)]}

    def test_quiet_watch_does_nothing(self):
        observed=[]
        self.assertFalse(producer.check_routes(self.watch,self.topology,5,observe=observed.append))
        self.assertEqual(observed,[])

    def test_notification_without_topology_change_keeps_the_generation(self):
        self.watch.notify(3)
        calls=[]
        def observe(deadline):calls.append(deadline);return dict(self.topology)
        self.assertTrue(producer.check_routes(self.watch,self.topology,7,observe=observe))
        self.assertEqual(calls,[7])
        # The burst was drained: a second check is quiet.
        self.assertFalse(producer.check_routes(self.watch,self.topology,7,observe=observe))
        self.assertEqual(calls,[7])

    def test_notification_with_topology_change_ends_the_generation(self):
        self.watch.notify()
        changed=dict(self.topology,neighbors=[dict(self.topology['neighbors'][0],lladdr='02:00:00:00:00:08')])
        with self.assertRaisesRegex(EventGap,'new session snapshot required'):
            producer.check_routes(self.watch,self.topology,7,observe=lambda deadline:changed)

    def test_without_reference_topology_any_notification_is_a_gap(self):
        self.watch.notify()
        with self.assertRaisesRegex(EventGap,'new session snapshot required'):
            producer.check_routes(self.watch,None,7,observe=lambda deadline:self.fail('no snapshot expected'))

    def test_lost_notifications_are_a_gap(self):
        self.watch.notify()
        with patch.object(self.watch,'recv',side_effect=OSError(105,'No buffer space available')):
            with self.assertRaisesRegex(EventGap,'overflowed'):
                producer.check_routes(self.watch,self.topology,7,observe=lambda deadline:self.topology)


class OwnershipTests(unittest.TestCase):
    """Labels travel in dumps and in the events that changed them; a later conntrack update
    carries none and must not read as a withdrawal (2026-10-19: every long-lived session whose
    last handshake event lacked labels vanished from the relay while still established)."""
    @staticmethod
    def row(event,token,**extra):
        return dict(event=event,id=7,zone=0,token=token,counters={},reply={},
                    original={'source':'10.0.0.1','destination':'10.0.0.2','protocol':6,'source_port':1,'destination_port':2},**extra)

    def test_update_without_labels_keeps_the_known_owner(self):
        row=self.row('update',None)
        self.assertEqual(producer.with_known_owner(row,'x',{'x':24577})['token'],24577)
        self.assertIsNone(row['token'])

    def test_update_without_labels_for_an_unknown_identity_stays_unowned(self):
        row=self.row('update',None)
        self.assertIsNone(producer.with_known_owner(row,'x',{'y':24577})['token'])

    def test_end_and_labelled_events_are_untouched(self):
        end=self.row('end',None);self.assertIs(producer.with_known_owner(end,'x',{'x':1}),end)
        labelled=self.row('update',9);self.assertIs(producer.with_known_owner(labelled,'x',{'x':1}),labelled)

    def test_replay_keeps_the_owner_across_label_less_updates(self):
        live=producer.replay([self.row('update',24577,tcp_state=2)],[self.row('update',None,tcp_state=3)])
        self.assertEqual([(r['token'],r['tcp_state']) for r in live],[(24577,3)])

    def test_replay_drops_ended_sessions_and_ignores_unowned_updates(self):
        self.assertEqual(producer.replay([self.row('update',24577)],[self.row('end',None)]),[])
        self.assertEqual(producer.replay([],[self.row('update',None)]),[])
        self.assertEqual([r['token'] for r in producer.replay([],[self.row('start',24577)])],[24577])


class RefreshTests(unittest.TestCase):
    """Conntrack reports counters in dumps and in create/destroy events, not while an established flow
    forwards (2026-10-21: 98 of 101 established rows showed identical counts 25 s apart and three regressed
    to nothing on a later event). A periodic dump refreshes the candidate rows' counts; nothing else."""
    BOOT='1b1582dd-a310-4cc1-85cd-0be7e600ae62'
    @staticmethod
    def dumped(ident,token=24577,state=3,packets=(10,20),**extra):
        return dict(event='update',id=ident,zone=0,token=token,tcp_state=state,timeout=431999,reply={},
                    original={'source':'10.0.0.1','destination':'10.0.0.2','protocol':6,'source_port':ident,'destination_port':443},
                    counters=dict(original=dict(packets=packets[0],bytes=packets[0]*1000),reply=dict(packets=packets[1],bytes=packets[1]*1400)),**extra)

    def emitted(self,row,candidate=True):
        return dict(identity=producer.identity_of(row,self.BOOT),token=row['token'],tcp_state=row['tcp_state'],
                    counters=row['counters'],software_candidate=candidate,remaining_seconds=1,l3={'available':True})

    def test_changes_leave_in_batches_the_receivers_accept(self):
        from session_stream import BATCH
        changes=[{'op':'close','identity':'i%d'%n} for n in range(BATCH*2+5)]
        out=producer.batches(changes)
        self.assertEqual([len(b) for b in out],[BATCH,BATCH,5])
        self.assertEqual(producer.batches([]),[])
        same=[{'op':'close','identity':'x'}]*(BATCH+9)
        self.assertEqual([len(b) for b in producer.batches(same)],[1],'coalesced first')

    def test_counters_known_needs_both_directions_with_packets_and_bytes(self):
        self.assertTrue(producer.counters_known(self.dumped(1)))
        for counters in ({},None,{'original':{'packets':1,'bytes':2}},{'original':{'packets':1},'reply':{'packets':1}}):
            self.assertFalse(producer.counters_known(dict(self.dumped(1),counters=counters)),counters)

    def test_identity_is_stable_and_follows_boot_zone_id_and_tuple(self):
        a=self.dumped(1);self.assertEqual(producer.identity_of(a,self.BOOT),producer.identity_of(dict(a,counters={}),self.BOOT))
        self.assertNotEqual(producer.identity_of(a,self.BOOT),producer.identity_of(self.dumped(2),self.BOOT))
        self.assertNotEqual(producer.identity_of(a,self.BOOT),producer.identity_of(a,'0'*36))

    def test_moved_candidate_rows_get_counter_only_upserts(self):
        before=self.dumped(1);emitted={e['identity']:e for e in [self.emitted(before)]}
        after=self.dumped(1,packets=(300,900))
        updates=producer.refreshed([after],emitted,self.BOOT)
        self.assertEqual(len(updates),1);row=updates[0]
        self.assertEqual((row['counters'],row['remaining_seconds'],row['identity']),(after['counters'],431999,producer.identity_of(after,self.BOOT)))
        self.assertEqual(row['l3'],{'available':True},'the plan is carried, not recomputed')

    def test_unchanged_unknown_non_candidate_and_changed_state_rows_are_left_to_the_event_path(self):
        base=self.dumped(1);emitted={e['identity']:e for e in [self.emitted(base),self.emitted(self.dumped(2),candidate=False)]}
        self.assertEqual(producer.refreshed([base],emitted,self.BOOT),[],'unchanged counters')
        self.assertEqual(producer.refreshed([self.dumped(9,packets=(5,5))],emitted,self.BOOT),[],'unknown identity')
        self.assertEqual(producer.refreshed([self.dumped(2,packets=(99,99))],emitted,self.BOOT),[],'not a candidate')
        self.assertEqual(producer.refreshed([self.dumped(1,packets=(99,99),state=4)],emitted,self.BOOT),[],'state changed: an event follows')
        self.assertEqual(producer.refreshed([self.dumped(1,packets=(99,99),token=5)],emitted,self.BOOT),[],'owner changed: an event follows')
        self.assertEqual(producer.refreshed([dict(self.dumped(1,packets=(99,99)),event='end')],emitted,self.BOOT),[],'an ended row')
        self.assertEqual(producer.refreshed([dict(self.dumped(1,packets=(99,99)),counters={})],emitted,self.BOOT),[],'no counters in the dump')


if __name__=='__main__':unittest.main()
