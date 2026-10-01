import os
from pathlib import Path
import unittest
from unittest.mock import patch
from ffn_fe100_lab_ports import profile
import ffn_fe100_bcm_lab as bcm


class LabPorts(unittest.TestCase):
    def test_explicit_board_wiring_and_separate_lif_slots(self):
        p=profile([23,24])
        self.assertEqual(p['physical'],{23:34,24:35})
        self.assertEqual(p['lif'],{23:28,24:29})
        self.assertEqual(profile([5,13])['lif'],{5:2,13:1})

    def test_invalid_port_selection(self):
        for pair in ([],[23],[23,23],[True,24],[0,24],[1,24],[23,25],['23',24]):
            with self.subTest(pair=pair),self.assertRaises(ValueError):profile(pair)

    def test_worker_inherits_exact_selected_pair(self):
        with patch.dict(os.environ,FFN_FE100_LAB_PAIR='23,24'):
            self.assertEqual(profile()['front'],[23,24])

    def test_dsa_and_route_recipes_use_selected_physical_ports(self):
        template=Path(__file__).resolve().parents[1]/'bcm/ffn_bcm_forward_test.c'
        with patch.object(bcm,'TEMPLATE',template):
            for mode in ('dsa-front13-create','dsa-front5-create','session-path-enable','front5-session-enable'):
                source=bcm.render(mode,{},[23,24])
                self.assertIn('int lab_port_a = 34;',source)
                self.assertIn('int lab_port_b = 35;',source)
            with self.assertRaises(ValueError):bcm.render('cross13-input-create',{},[23,24])

    def test_queue_ids_require_current_selected_destinations(self):
        calls=[]
        def execute(source,call):
            calls.append(source)
            return {'markers':['FFN_LAB_QUEUE port=%d qid=%d count=8'%(p,p*8) for p in (3,8,24,34,35)]}
        with patch.object(bcm,'execute',execute):
            result=bcm.queue_ids(None,[23,24])
        self.assertEqual(set(result['queue_ids']),{3,8,24,34,35})
        self.assertIn('port==34',calls[0]);self.assertNotIn('port==16',calls[0])


if __name__=='__main__':unittest.main()
