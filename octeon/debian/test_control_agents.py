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
    def driver_fixture(self, root):
        def write(name, value):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value if isinstance(value, bytes) else value.encode())
        write('usr/local/sbin/ffn_fe100.py', "PCI_DEV = '0002:01:00.0'\nraise RuntimeError('must never execute')\n")
        write('usr/local/sbin/ffn_fe100_lookup_health.py', '# fixture')
        write('opt/ffn-compat/opt/ffn/fe100-csr.json', '[]')
        write('dev/mem', b'')
        prefix = 'sys/bus/pci/devices/0002:01:00.0/'
        write(prefix + 'vendor', '0xfeed'); write(prefix + 'device', '0xfe1c')
        write(prefix + 'resource', '100000 1fffff 200\n')
        write(prefix + 'config', b'\0\0\0\0\x02\0')
        return root / prefix

    def test_fe100_userspace_installation_is_not_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); device = self.driver_fixture(root)
            observed = cp.fe100_driver_status(root)
            self.assertEqual(observed['devices'][0]['kernel_state'], 'unbound')
            self.assertTrue(observed['userspace']['installed'])
            self.assertEqual(observed['userspace']['state'], 'installed-unverified')
            self.assertEqual(len(observed['userspace']['sha256']), 64)
            cp.qualify_fe100_access(observed, {'available': True})
            self.assertTrue(observed['userspace']['read_verified'])
            self.assertFalse(observed['forwarding_verified'])
            cp.qualify_fe100_access(observed, {'available': False})
            self.assertFalse(observed['userspace']['read_verified'])
            device.joinpath('config').write_bytes(b'\0'*6)
            observed = cp.qualify_fe100_access(cp.fe100_driver_status(root), {'available':True})
            self.assertFalse(observed['userspace']['read_verified'])

    def test_kernel_binding_and_unreadable_or_wrong_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); device = self.driver_fixture(root)
            with patch.object(Path, 'readlink', side_effect=[Path('/drivers/vfio-pci'), Path('/module/vfio_pci')]):
                observed = cp.fe100_driver_status(root)
            self.assertEqual(observed['devices'][0]['kernel_driver'], 'vfio-pci')
            observed['userspace']['target_pci'] = '0002:02:00.0'
            self.assertFalse(cp.qualify_fe100_access(observed, {'available':True})['userspace']['read_verified'])
            device.joinpath('config').write_bytes(b'\0')
            observed = cp.fe100_driver_status(root)
            self.assertIsNone(observed['devices'][0]['memory_decode'])
            self.assertTrue(observed['errors'])
            root.joinpath('usr/local/sbin/ffn_fe100.py').unlink()
            self.assertFalse(cp.fe100_driver_status(root)['userspace']['installed'])

    def test_driver_probe_failure_does_not_hide_chip_or_counter_status(self):
        with patch.object(cp.Path, 'read_text', return_value='octeon'), \
             patch.object(cp, 'fe100_driver_status', side_effect=OSError), \
             patch.object(cp, 'bcm_status', return_value={'available':True}), \
             patch.object(cp, 'fe100_status', return_value={'available':True}), \
             patch.object(cp, 'policy_status', return_value={'available':True}):
            report = cp.snapshot()
        self.assertTrue(report['fe100']['available'])
        self.assertFalse(report['fe100_driver']['userspace']['read_verified'])

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

    def test_recovery_freshness_boot_and_generation_fences(self):
        report = {'schema': 1, 'cp_boot_id': 'boot', 'monotonic_time': 100,
                  'outcome': 'drained', 'revision': 5, 'sessions': 0}
        def observed(value=report, now=105, revision=5, count=0, phase='blocked'):
            with patch.object(cp.Path, 'read_text', side_effect=[json.dumps(value), 'boot']), \
                    patch.object(cp.time, 'monotonic', return_value=now):
                return cp.policy_recovery_status('/unused', revision, count, phase)
        self.assertTrue(observed()['drain_verified'])
        for value in [report | {'cp_boot_id': 'old'}, report | {'outcome': 'busy'},
                      report | {'revision': 4}, report | {'sessions': 1}]:
            self.assertFalse(observed(value)['drain_verified'])
        for args in [{'now': 200}, {'now': 99}, {'count': 1}, {'phase': 'draining'}]:
            self.assertFalse(observed(**args)['drain_verified'])

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
