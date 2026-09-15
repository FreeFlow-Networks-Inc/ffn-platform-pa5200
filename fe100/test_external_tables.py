import ctypes as C
import unittest
from ffn_fe100_external_tables import IndirectAccess, cfg4_v4_v6, initialize


class Transport:
    def __init__(self,fail=None,verify=True):
        self.calls=[]; self.fail=fail; self.verify=verify; self.progress=[]
        self.ready={k:True for k in ('exclusive','ddr_training_verified',
            'ddr_memory_test_verified','tcam_ready','packet_lookup_quiescent')}
    def status(self): return self.ready
    def begin(self,*args): self.progress.append(('begin',args))
    def ia_op(self,dev,block,request):
        self.calls.append((dev,block,request.addr,tuple(request.data[i] for i in range(3))))
        return 7 if len(self.calls)==self.fail else 0
    def verify_configuration(self,entries): return self.verify
    def complete(self,count): self.progress.append(('complete',count))
    def failed(self,count): self.progress.append(('failed',count))


class ExternalTables(unittest.TestCase):
    def test_abi(self):
        self.assertEqual(C.sizeof(IndirectAccess),64)
        self.assertEqual(IndirectAccess.addr.offset,32)
        self.assertEqual(IndirectAccess.data.offset,48)
        request,data=cfg4_v4_v6()[0].native()
        self.assertEqual((request.trgt_mem,request.acc_type,request.acc_size,request.dcnt),(256,2,3,3))

    def test_requires_memory_and_quiescence(self):
        for field in Transport().ready:
            t=Transport(); t.ready[field]=False
            with self.assertRaises(RuntimeError): initialize(t)
            self.assertEqual(t.calls,[])
            self.assertEqual(t.progress,[])

    def test_partial_failure_never_marks_complete_or_continues(self):
        t=Transport(fail=17)
        with self.assertRaises(RuntimeError): initialize(t)
        self.assertEqual(len(t.calls),17)
        self.assertEqual(t.progress[-1],('failed',16))
        t=Transport(verify=False)
        with self.assertRaises(RuntimeError): initialize(t)
        self.assertEqual(t.progress[-1],('failed',263))

    def test_success_does_not_claim_session_offload(self):
        t=Transport()
        self.assertFalse(initialize(t)['session_offload_verified'])
        self.assertEqual(len(t.calls),263)
        self.assertEqual(t.progress[-1],('complete',263))
        self.assertEqual(t.calls[256][2],0x2fe0)
        self.assertEqual(t.calls[-1][2:],(0x40a5c,(0,0,0xe83c8780)))


if __name__=='__main__': unittest.main()
