import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ffn_packet_cpus import CpuReservations


def claim(root,queue,release):
    allocator=CpuReservations(root,Path(root)/'missing-status')
    token,cpus=allocator.reserve(range(9))
    queue.put(cpus);release.wait(10);allocator.release(token)


class PacketCpuTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.run=self.root/'run';self.run.mkdir()
        self.a=CpuReservations(self.root/'claims',self.run)

    def test_spare_cores_low_core_masks_and_release(self):
        first,cpus=self.a.reserve([0,1,2,3,4]);self.assertEqual(cpus,(1,2))
        second,cpus=self.a.reserve([0,1,2,3,4]);self.assertEqual(cpus,(3,4))
        self.a.release(first)
        third,cpus=self.a.reserve([0,1,2,3,4]);self.assertEqual(cpus,(1,2))
        self.a.release(second);self.a.release(third)
        token,cpus=self.a.reserve([7,11]);self.assertEqual(cpus,(7,11));self.a.release(token)
        token,cpus=self.a.reserve([7]);self.assertEqual(cpus,(7,7));self.a.release(token)

    def test_restricted_mask_balances_oversubscription(self):
        with patch('ffn_packet_cpus.os.sched_getaffinity',return_value={4,8,12}):
            self.assertEqual(self.a.reserve()[1],(8,12))
            self.assertEqual(self.a.reserve()[1],(8,12))

    def test_reused_pid_and_previous_boot_are_pruned(self):
        for field in ('process_start','boot_id'):
            token,_=self.a.reserve(range(5));path=self.a.root/(token+'.json')
            row=json.loads(path.read_text());row[field]='not-current';path.write_text(json.dumps(row))
            current,cpus=self.a.reserve(range(5));self.assertEqual(cpus,(1,2))
            self.assertFalse(path.exists());self.a.release(current)

    def test_legacy_owner_requires_process_and_thread_identity(self):
        row=dict(self.a.identity(os.getpid()),workers=dict(rx_tid=os.getpid(),tx_tid=os.getpid()))
        (self.run/'ffn-fabric.json').write_text(json.dumps(row))
        with patch('ffn_packet_cpus.os.sched_getaffinity',return_value={1}):
            token,cpus=self.a.reserve(range(5));self.assertEqual(cpus,(2,3));self.a.release(token)
            row['process_start']='reused';(self.run/'ffn-fabric.json').write_text(json.dumps(row))
            self.assertEqual(self.a.reserve(range(5))[1],(1,2))

    def test_concurrent_owners_reserve_distinct_cores(self):
        ctx=mp.get_context('spawn');queue=ctx.Queue();release=ctx.Event()
        children=[ctx.Process(target=claim,args=(str(self.a.root),queue,release)) for _ in range(4)]
        try:
            for child in children:child.start()
            cpus=[cpu for _ in children for cpu in queue.get(timeout=15)]
            self.assertEqual(sorted(cpus),list(range(1,9)))
        finally:
            release.set()
            for child in children:
                child.join(15)
                if child.is_alive():child.terminate();child.join()
                self.assertEqual(child.exitcode,0)

    def test_invalid_affinity_and_token(self):
        with self.assertRaises(ValueError):self.a.reserve([])
        with self.assertRaises(ValueError):self.a.reserve([-1])
        with self.assertRaises(ValueError):self.a.release('../lock')


if __name__=='__main__':unittest.main()
