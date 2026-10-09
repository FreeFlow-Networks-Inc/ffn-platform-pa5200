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


if __name__=='__main__':unittest.main()
