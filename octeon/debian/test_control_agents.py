import json
import unittest
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import ffn_cp_agent as cp
import ffn_dp_agent as dp


class Agents(unittest.TestCase):
    def test_session_journal_is_read_only_and_not_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'policy.db'
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE policy_state (id INTEGER,body TEXT)')
            db.execute('CREATE TABLE sessions (id INTEGER,body TEXT)')
            db.execute('INSERT INTO policy_state VALUES (1,?)',(json.dumps({'revision':5,'phase':'active'}),))
            db.execute('INSERT INTO sessions VALUES (1,?)',('pending',));db.commit();db.close()
            before=path.read_bytes();report=cp.policy_status(path)
            self.assertEqual(report['journaled_sessions'],1)
            self.assertEqual(report['configured_revision'],5)
            self.assertFalse(report['hardware_activation_verified'])
            self.assertEqual(path.read_bytes(),before)

    def test_fe100_reports_nonclearing_counters_without_register_dump(self):
        observed={'summary':{'offload_verified':False}, 'samples':[
            {'monotonic_time':1}, {'monotonic_time':1.1,'registers':{
                'acl_stats_ctr_no_rd_clr':{'raw':17},'tdi_init_status':{'raw':9}}}]}
        process=AsyncMock(return_value=observed)
        with patch.dict('sys.modules', {'ffn_planed':SimpleNamespace(process=process)}):
            report=cp.fe100_status()
        self.assertEqual(report['counters'],{'acl_stats_ctr_no_rd_clr':17})
        self.assertFalse(report['offload_verified'])
        self.assertNotIn('registers',report)
        self.assertIn('--samples',process.await_args.args[0])

    def test_cp_partial_observer_failure_preserves_other_state(self):
        def read(path):
            return 'OCTEON' if path.as_posix()=='/proc/cpuinfo' else 'boot'
        with patch.object(cp.Path,'read_text',read), patch.object(cp,'bcm_status',return_value={'available':True}), \
                patch.object(cp,'fe100_status',side_effect=OSError), patch.object(cp,'policy_status',return_value={'available':True}):
            report=cp.snapshot()
        self.assertTrue(report['ready']); self.assertFalse(report['fe100']['available'])
        self.assertFalse(report['forwarding_verified'])

    def test_inactive_vif_does_not_open_or_start_an_owner(self):
        with patch.object(dp.Path,'exists',return_value=False), patch.object(dp.socket,'socket') as sock:
            result=dp.vif_status()
        sock.assert_not_called();self.assertFalse(result['forwarding'])
        self.assertIs(result['running'],False)

    def test_vif_socket_error_is_not_inactive_or_forwarding(self):
        with patch.object(dp.Path,'exists',return_value=True), patch.object(dp.socket,'socket',side_effect=OSError), \
                patch.object(dp.socket,'AF_UNIX',1,create=True):
            result=dp.vif_status()
        self.assertIsNone(result['running']);self.assertFalse(result['forwarding'])


if __name__=='__main__':unittest.main()
