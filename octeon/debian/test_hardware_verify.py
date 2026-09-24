import unittest
from ffn_hardware_verify import evaluate

class VerificationTests(unittest.TestCase):
    def snapshot(self):
        return {'cp_boot_id':'current', 'pid1':'/usr/lib/systemd/systemd',
                'fe100_pci_driver':'ffn_fe100',
                'bcm':{'state':'ready','init_errors':[]},
                'ports':[{'port':p,'link':True} for p in (3,20,24)],
                'copper':[{'phy':p,'id':[0x600d,0x84f9],'firmware':0x1089,'reset':False} for p in range(16,20)],
                'mdio_write_gates':{'allow_writes':False,'allow_gearbox_writes':False},
                'fe100':{'cp_boot_id':'current','initialized':True,'blockers':[],'action_blockers':[]}}
    def test_initialization_never_claims_packet_qualification(self):
        result=evaluate(self.snapshot())
        self.assertTrue(result['initialization_verified'])
        self.assertFalse(result['session_table_verified'])
        self.assertFalse(result['physical_forwarding_verified'])
        self.assertFalse(result['production_offload_qualified'])
    def test_session_test_requires_matching_generation_and_cleanup(self):
        snapshot = self.snapshot()
        generation = {'epoch':'reset-2','cp_boot_id':'current'}
        passed = {'hardware_generation':generation,'cp_boot_id':'current',
                  'stage':'completed','session_table_verified':True,
                  'cleanup_verified':True,'faults':0}
        snapshot.update(hardware_generation=generation, session_validation=passed)
        self.assertTrue(evaluate(snapshot)['session_table_verified'])
        for change in ({'hardware_generation':{'epoch':'reset-1','cp_boot_id':'current'}},
                       {'cp_boot_id':'old'}, {'stage':'cleanup-required'},
                       {'cleanup_verified':False}, {'faults':1}):
            snapshot['session_validation'] = passed | change
            self.assertFalse(evaluate(snapshot)['session_table_verified'])
    def test_stale_boot_or_failure_cannot_pass(self):
        for change in ('boot','copper','link','gate','memory','driver'):
            with self.subTest(change=change):
                s=self.snapshot()
                if change=='driver':s['fe100_pci_driver']=None
                if change=='boot':s['fe100']['cp_boot_id']='previous'
                if change=='copper':s['copper'].pop()
                if change=='link':s['ports'][0]['link']=False
                if change=='gate':s['mdio_write_gates']['allow_writes']=True
                if change=='memory':s['fe100']['action_blockers']=['unverified FCM']
                self.assertFalse(evaluate(s)['initialization_verified'])
if __name__=='__main__':unittest.main()
