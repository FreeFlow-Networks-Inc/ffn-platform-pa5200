import copy
import unittest
from validate_physical_sessions import qualifies, frames, checksum


class Qualification(unittest.TestCase):
    def setUp(self):
        self.phases={p:{'count':1,'original':[],'rewritten':[],
                       'packets':[],'counter_delta':{}}
                     for p in ('baseline','miss','hit','drop','removed')}
        for p,kind in (('baseline','original'),('hit','rewritten')):
            self.phases[p][kind]=[0]
            packet={'original':[],'rewritten':[]};packet[kind]=[0]
            self.phases[p]['packets']=[packet]
        for p in ('miss','removed'):
            self.phases[p]['counter_delta']['dfp_flow_lkup_miss_sta_ctr_no_rd_clr']=1
        for p in ('hit','drop'):
            self.phases[p]['counter_delta']['dfp_flow_lkup_flow_hit_sta_ctr_no_rd_clr']=1
        self.phases['hit']['counter_delta'].update({k:1 for k in (
            'dfp_flow_ct_sta_ctr_no_rd_clr','fwd_dir_lkup_req_sta_ctr_no_rd_clr',
            'flu_sem_inc_instr_ctr_no_rd_clr')})
        self.phases['drop']['counter_delta']['dfp_flow_drop_sta_ctr_no_rd_clr']=1

    def test_requires_packet_and_hardware_evidence(self):
        self.assertTrue(qualifies(self.phases))
        for phase in ('miss','hit','drop','removed'):
            bad=copy.deepcopy(self.phases);bad[phase]['counter_delta']={}
            self.assertFalse(qualifies(bad),phase)

    def test_duplicates_corrupt_drop_and_stale_forwarding_fail(self):
        for phase in ('baseline','hit'):
            bad=copy.deepcopy(self.phases);bad[phase]['packets']*=2
            self.assertFalse(qualifies(bad))
        bad=copy.deepcopy(self.phases)
        bad['drop']['packets']=[{'original':[],'rewritten':[]}]
        self.assertFalse(qualifies(bad))
        bad=copy.deepcopy(self.phases);bad['removed']['rewritten']=[0]
        self.assertFalse(qualifies(bad))

    def test_probe_checksums_and_unique_sequences(self):
        packets=frames('12'*16,4)
        self.assertEqual(len(set(packets)),4)
        for p in packets:
            self.assertEqual(checksum(p[14:34]),0)
            udp=p[34:]
            self.assertEqual(checksum(p[26:34]+bytes([0,17])+len(udp).to_bytes(2,'big')+udp),0)


if __name__=='__main__':unittest.main()
