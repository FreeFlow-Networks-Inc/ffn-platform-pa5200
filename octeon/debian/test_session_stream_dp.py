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


if __name__=='__main__':unittest.main()
