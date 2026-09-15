import unittest
from ffn_fe100_tcam_sync import synchronize,NOP_DONE
from ffn_fe100_clocks import RST,INIT,CLOCK_MASK,TCAM_STATUS
from ffn_fe100_hardware_status import journal_health
from ffn_fe100_external_runtime import ExternalTransport
from ffn_fe100_tcam_recovery import require_idle_traffic
from unittest.mock import Mock


class IO:
    def __init__(self,complete=True):
        self.values={RST:0x4d81,INIT:CLOCK_MASK|(3<<21),TCAM_STATUS:1}
        self.writes=[]; self.complete=complete
    def read(self,r): return self.values[r]
    def write(self,r,v):
        self.writes.append((r,v)); self.values[r]=v
        if r==RST and v&0x200 and self.complete: self.values[INIT]|=NOP_DONE


class RecoveryTests(unittest.TestCase):
    def test_sync_preserves_ddr_and_restores_control(self):
        io=IO()
        self.assertTrue(synchronize(io,sleep=lambda _:None)['changed'])
        self.assertEqual(io.writes,[(RST,0x4f81),(RST,0x4d81)])
        self.assertTrue(io.read(INIT)&NOP_DONE)

    def test_sync_timeout_bounded_and_restored(self):
        io=IO(False); sleeps=[]
        with self.assertRaises(TimeoutError): synchronize(io,sleep=sleeps.append)
        self.assertEqual(len(sleeps),501)
        self.assertEqual(io.read(RST),0x4d81)

    def test_already_synced_no_write(self):
        io=IO(); io.values[INIT]|=NOP_DONE
        self.assertFalse(synchronize(io)['changed']); self.assertEqual(io.writes,[])

    def test_unknown_reset_state_untouched(self):
        io=IO(); io.values[RST]|=0x20
        with self.assertRaises(RuntimeError): synchronize(io)
        self.assertEqual(io.writes,[])

    def test_interrupted_asserted_control_not_silently_accepted(self):
        io=IO(); io.values[RST]|=0x200; io.values[INIT]|=NOP_DONE
        with self.assertRaises(RuntimeError): synchronize(io)
        self.assertEqual(io.writes,[])

    def test_active_traffic_blocks_recovery(self):
        io=Mock(); io.read.side_effect=[0]*5+[0,0,1,0,0]
        with self.assertRaises(RuntimeError): require_idle_traffic(io,sleep=lambda _:None)
        io.write.assert_not_called()

    def test_interruption_restores_nop_control(self):
        io=IO()
        def interrupted(_): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): synchronize(io,sleep=interrupted)
        self.assertEqual(io.read(RST),0x4d81)

    def test_stale_success_cannot_clear_recovery(self):
        j={'stage':'configuration-verified','cp_boot_id':'old'}
        self.assertTrue(journal_health(j,'new',True,True)['recovery_required'])
        self.assertTrue(journal_health(j,'old',True,False)['recovery_required'])
        self.assertTrue(journal_health(j,'old',False,True)['recovery_required'])
        self.assertFalse(journal_health(j,'old',True,True)['recovery_required'])
        j['stage']='recovering'
        self.assertTrue(journal_health(j,'old',True,True)['recovery_required'])

    def transport(self,overrides=None):
        io=Mock()
        io.shim.ffn_fe100_allow_external_ia.return_value=0
        io.shim.ffn_fe100_allow_readonly.return_value=0
        io.shim.ffn_fe100_allow.return_value=0
        values={0xa0708:3,0xa0214:5,0xa0224:6}
        values.update(overrides or {})
        io.read.side_effect=lambda r:values.get(r,0)
        return ExternalTransport(io)

    def test_receive_fill_is_not_packet_work(self):
        self.assertTrue(self.transport().quiescent())

    def test_pending_work_and_receive_faults_block(self):
        for addr,value in ((0x80794,1),(0xa01e4,1),(0xa0218,0x8000),(0xa0224,16)):
            self.assertFalse(self.transport({addr:value}).quiescent())


if __name__=='__main__': unittest.main()
