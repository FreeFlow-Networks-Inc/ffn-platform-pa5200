import unittest
from ffn_fe100_sessions import key4, entry4, readiness, SessionManager


class Backend:
    def __init__(self): self.rows={}; self.writes=0; self.fail=0; self.blocked=[]
    def readiness(self): return self.blocked
    def fetch(self,key): return self.rows.get(key)
    def insert(self,entry):
        self.rows[entry[:16]]=entry; self.writes+=1
        if self.writes==self.fail: raise TimeoutError('ambiguous completion')
    def delete(self,key): self.rows.pop(key,None)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.entries=[entry4(key4(a,b,s,d,17,23),101+i) for i,(a,b,s,d) in enumerate([
            ('192.0.2.1','192.0.2.2',1000,2000),('192.0.2.2','192.0.2.1',2000,1000)])]
        self.backend=Backend(); self.manager=SessionManager(self.backend)

    def test_wire_vector(self):
        self.assertEqual(self.entries[0][:16].hex(),'4011001703e807d0c0000201c0000202')
        self.assertEqual(len(self.entries[0]),64)
        self.assertEqual(self.entries[0][36:40],b'\0\0\0e')

    def test_install_and_policy_invalidation(self):
        self.manager.install(1,self.entries,9)
        self.assertEqual(len(self.backend.rows),2)
        self.manager.invalidate_policy(10)
        self.assertFalse(self.backend.rows)
        self.assertFalse(self.manager.sessions)

    def test_second_ambiguous_write_rolls_back_both(self):
        self.backend.fail=2
        with self.assertRaises(TimeoutError): self.manager.install(1,self.entries,9)
        self.assertFalse(self.backend.rows)
        self.assertFalse(self.manager.recovery_required)

    def test_no_overwrite_or_unqualified_writes(self):
        self.backend.rows[self.entries[1][:16]]=self.entries[1]
        with self.assertRaises(RuntimeError): self.manager.install(1,self.entries,9)
        self.assertEqual(self.backend.writes,0)
        self.backend.rows.clear(); self.backend.blocked=['clocks off']
        with self.assertRaises(RuntimeError): self.manager.install(1,self.entries,9)
        self.assertEqual(self.backend.writes,0)

    def test_failed_cleanup_requires_recovery(self):
        self.backend.fail=2
        self.backend.delete=lambda key: None
        with self.assertRaises(TimeoutError): self.manager.install(1,self.entries,9)
        self.assertTrue(self.manager.recovery_required)
        self.assertEqual(self.manager.sessions[1]['state'],'unknown')
        with self.assertRaises(RuntimeError): self.manager.install(2,self.entries,9)

    def test_missing_health_fails_closed(self):
        self.assertEqual(len(readiness({},False)),3)
        with self.assertRaises(ValueError): key4('192.0.2.1','192.0.2.2',True,80,6,1)

    def test_unqualified_actions_and_mismatched_pair_rejected(self):
        for offset in (16, 40, 63):
            changed=bytearray(self.entries[0]); changed[offset]=1
            with self.assertRaises(ValueError): self.manager.install(1,[changed,self.entries[1]],9)
        other=entry4(key4('192.0.2.3','192.0.2.1',2000,1000,17,23),103)
        with self.assertRaises(ValueError): self.manager.install(1,[self.entries[0],other],9)
        self.assertEqual(self.backend.writes,0)


if __name__=='__main__': unittest.main()
