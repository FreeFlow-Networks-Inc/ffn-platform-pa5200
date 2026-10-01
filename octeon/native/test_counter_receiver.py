"""Privileged native receiver process tests; run inside an empty network namespace.

No packets or hardware writes. Exercises the actual AF_PACKET observer and
its control pipe, including a stopped receiver and a consumer that stops reading.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'fe100'))
from ffn_fe100_flowstats import NativeCounterStream


class ReceiverTests(unittest.TestCase):
    def start(self,duration=1000):
        child=subprocess.Popen([str(Path(__file__).with_name('ffn-fe100-stats-probe')),
            'lo','24','20',str(duration),'--health'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.addCleanup(self.stop,child)
        return child

    @staticmethod
    def stop(child):
        if child.poll() is None:child.kill()
        child.communicate(timeout=3)

    def test_idle_health_never_claims_packet_activity(self):
        child=self.start();out,err=child.communicate(timeout=5)
        self.assertEqual(child.returncode,0,err)
        rows=[json.loads(line) for line in out.splitlines()]
        self.assertEqual(rows[0],{'ready':True})
        health=rows[1:-1]
        self.assertGreaterEqual(len(health),3)
        self.assertTrue(all(row['health'] is True for row in health))
        self.assertEqual([r['sequence'] for r in health],list(range(1,len(health)+1)))
        self.assertEqual(rows[-1],dict(messages=0,records=0,malformed=0,capture_drops=0,failed=False))

    def test_stopped_native_process_cannot_keep_owner_healthy(self):
        child=self.start(10000)
        self.assertEqual(json.loads(child.stdout.readline()),{'ready':True})
        stream=NativeCounterStream('isolated-receiver',receiver_timeout=.5)
        stream.consume(stream.epoch,json.loads(child.stdout.readline()))
        stream.check_receiver()
        child.send_signal(signal.SIGSTOP)
        time.sleep(.6)
        with self.assertRaises(RuntimeError):stream.check_receiver()
        self.assertFalse(stream.available)

    def test_closed_consumer_ends_native_process(self):
        child=self.start(60000)
        child.stdout.close();child.stdout=None
        child.wait(timeout=3)
        self.assertNotEqual(child.returncode,0)

    def test_blocked_consumer_does_not_hang_native_process(self):
        child=self.start(60000)
        fcntl.fcntl(child.stdout.fileno(),fcntl.F_SETPIPE_SZ,4096)
        child.wait(timeout=40)
        self.assertNotEqual(child.returncode,0)


if __name__=='__main__':
    assert os.readlink('/proc/self/ns/net')!=os.readlink('/proc/1/ns/net')
    import socket
    assert {name for _,name in socket.if_nameindex()} <= {'lo','sit0'}
    subprocess.run(['ip','link','set','lo','up'],check=True)
    unittest.main(verbosity=2)
