import ast
import json
from pathlib import Path
import unittest
import tempfile
import build_images


class RuntimeOverlayTests(unittest.TestCase):
    def test_boot_enables_infrastructure_without_packet_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'etc/systemd/system'
            directory.mkdir(parents=True)
            for name in ('ffn-network.service', 'ffn-interface-services.service', 'ffn-static-routes.service', 'ffn-security-runtime.service', 'ffn-aggregate-dp-watchdog.timer'):
                (directory / name).write_text('[Unit]\n')
            build_images.enable_runtime_units(root, 'dp', {})
            build_images.enable_runtime_units(root, 'dp', {})
            links = list(directory.glob('*.wants/*'))
            self.assertEqual({p.name for p in links}, {'ffn-network.service', 'ffn-interface-services.service', 'ffn-static-routes.service', 'ffn-security-runtime.service', 'ffn-aggregate-dp-watchdog.timer'})
            self.assertTrue(all(p.is_symlink() and p.is_file() for p in links))

    def test_boot_rejects_redirected_unit_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'; root.mkdir()
            (root / 'etc').symlink_to(Path(tmp), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                build_images.enable_runtime_units(root, 'cp', {})

    def test_dp_packet_owners_and_their_platform_imports_are_packaged(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root / 'octeon/images/overlay.json').read_text())
        files = {Path(dest).name: (origin, source) for origin, source, dest in manifest['common'] + manifest['dp']}
        for name in ('ffn_network.py', 'ffn_network_init.py', 'ffn_wan_runtime.py',
                     'ffn_wan_probe.py', 'ffn_aggregate_runtime.py', 'ffn_vif_runtime.py',
                     'ffn_interface_addresses.py', 'ffn_controld_client.py',
                     'ffn_platform_policy_bindings.py',
                     'ffn-network.service', 'ffn-wan-attachment.service'):
            self.assertIn(name, files)
        for name, (origin, source) in files.items():
            if origin != 'platform' or not name.endswith('.py'):
                continue
            tree = ast.parse((root / source).read_text())
            for node in ast.walk(tree):
                imports = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                for module in imports:
                    if module.startswith('ffn_'):
                        self.assertIn(module + '.py', files, name + ' requires ' + module)
        cp = {Path(dest).name for _, _, dest in manifest['cp']}
        self.assertTrue({'ffn_copper_link.py', 'ffn-copper-link.service', 'ffn-copper-link.timer'} <= cp)
        self.assertTrue({'ffn-aggregate-watchdog.service', 'ffn-aggregate-watchdog.timer'} <= cp)
        self.assertTrue({'ffn_port_led_enable.py', 'ffn-port-led-enable.service'} <= cp)
        self.assertTrue({'ffn_bcm_reference.py', 'ffn_bcm_trunk.py'} <= cp)
        self.assertIn('ffn_vrrp.py', files)
        self.assertTrue({'ffn_interface_services.py','ffn_interface_profile_source.py','ffn-interface-services.service'} <= files.keys())
        self.assertTrue({'ffn_port_events.py','ffn_front_traffic.py','ffn-port-events.service'} <= cp)
        self.assertTrue({'ffn_interface_services.py','ffn_interface_profile_source.py','ffn-interface-services.service'} <= files.keys())
        self.assertTrue({'ffn_port_events.py','ffn_front_traffic.py','ffn-port-events.service'} <= cp)

    def test_cp_boot_enables_front_led_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); directory = root / 'etc/systemd/system'; directory.mkdir(parents=True)
            for name in ('ffn-copper-link.timer', 'ffn-fe100-recovery.timer', 'ffn-aggregate-watchdog.timer', 'ffn-port-led-enable.service', 'ffn-port-events.service'):
                (directory / name).write_text('[Unit]\n')
            build_images.enable_runtime_units(root, 'cp', {})
            self.assertTrue((directory / 'multi-user.target.wants/ffn-port-led-enable.service').is_file())


if __name__ == '__main__':
    unittest.main()
