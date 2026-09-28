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
            for name in ('ffn-network.service', 'ffn-aggregate-dp-watchdog.timer'):
                (directory / name).write_text('[Unit]\n')
            build_images.enable_runtime_units(root, 'dp', {})
            build_images.enable_runtime_units(root, 'dp', {})
            links = list(directory.glob('*.wants/*'))
            self.assertEqual({p.name for p in links}, {'ffn-network.service', 'ffn-aggregate-dp-watchdog.timer'})
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


if __name__ == '__main__':
    unittest.main()
