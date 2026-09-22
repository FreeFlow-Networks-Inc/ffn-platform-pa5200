import unittest
from validate_front_sessions import expected_return
from validate_nat_results import audit,summarize,CASES
from ffn_fe100_clocks import SHA


def fixture(protocol='udp',mode='address',ingress=13):
    from test_physical_sessions import Qualification
    test=Qualification();test.setUp();phases=test.phases
    for p in phases.values():
        p.update(count=4,capture_drops=0,dp_capture_drops=0)
        p['counter_delta']={k:4 for k in p['counter_delta']}
    for phase,kind in (('baseline','original'),('hit','rewritten')):
        phases[phase][kind]=list(range(4));phases[phase]['packets']=[]
        for i in range(4):
            packet=dict(original=[],rewritten=[]);packet[kind]=[i];phases[phase]['packets'].append(packet)
    phases['hit']['token']='12'*16
    header=bytearray(32);header[24:28]=(ingress<<22).to_bytes(4,'big')
    for p,frame in zip(phases['hit']['packets'],expected_return('12'*16,4,ingress==5,mode,protocol)):
        p['raw']=(bytes(header)+frame).hex()
    return dict(protocol=protocol,nat_mode=mode,ingress=ingress,egress=18-ingress,
                nat_direction_verified=True,session_offload_verified=True,production_nat_qualified=False,
                phases=phases,cp_cleanup=dict(restored=True),baseline_cleanup=dict(restored=True,baseline=dict(epoch='boot:1:2')),
                controller=dict(hardware=dict(cp_boot_id='boot',owner_sha256=SHA)),production_owners={'ae1':'owner'})


class NatResultsTests(unittest.TestCase):
    def test_matrix_is_evidence_only_even_when_complete(self):
        result=summarize([fixture(*case) for case in sorted(CASES)])
        self.assertTrue(result['complete']);self.assertFalse(result['production_admission'])
        self.assertFalse(result['tcp_state_tracking_verified']);self.assertEqual(result['missing'],[])
        self.assertFalse(summarize([fixture()])['complete'])

    def test_corrupt_bytes_metadata_cleanup_and_capture_loss_rejected(self):
        for mutate in (lambda r:r['phases']['hit']['packets'][0].update(raw='00'*32),
                       lambda r:r['baseline_cleanup'].update(restored=False),
                       lambda r:r['phases']['hit'].update(capture_drops=1),
                       lambda r:r['phases']['hit'].pop('dp_capture_drops'),
                       lambda r:r['phases']['baseline'].update(capture_drops=False),
                       lambda r:r.update(error='interrupted'),
                       lambda r:r['controller']['hardware'].update(cp_boot_id='other')):
            report=fixture();mutate(report)
            with self.assertRaises(ValueError):audit(report)
        report=fixture();packet=report['phases']['hit']['packets'][0]
        raw=bytearray.fromhex(packet['raw']);raw[-1]^=1;packet['raw']=raw.hex()
        with self.assertRaisesRegex(ValueError,'checksums'):audit(report)

    def test_duplicates_and_owner_changes_cannot_be_combined(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):summarize([fixture(),fixture()])
        other=fixture(ingress=5);other['production_owners']={'ae1':'new-owner'}
        with self.assertRaisesRegex(ValueError,'lifetimes'):summarize([fixture(),other])


if __name__=='__main__':unittest.main()
