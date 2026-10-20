"""The restart owner's preflight: a plain restart keeps the running image, an activation requires a new one."""
import unittest
from unittest.mock import patch

import plane_restart_owner as owner


def observe(role, notes, boot='boot-1'):
    return {'boot_id': boot, 'notes_sha256': notes, 'kernel_release': role + '-kernel'}


class Inspect(unittest.TestCase):
    def run_inspect(self, selected_notes, running_notes, activation):
        with patch.object(owner, 'profile', lambda role: {'role': role}), \
             patch.object(owner, 'preflight', lambda cfg: {'notes_sha256': selected_notes, 'pci': '0000:01:00.0'}), \
             patch.object(owner, 'remote', lambda program, *args, **kw: observe(args[1], running_notes)):
            return owner.inspect('cp', activation=activation)

    def test_restart_refuses_a_kernel_change(self):
        with self.assertRaises(ValueError) as caught:
            self.run_inspect('new', 'old', activation=False)
        self.assertIn('image activation', str(caught.exception))
        self.assertEqual(self.run_inspect('same', 'same', activation=False)['image_activation'], False)

    def test_activation_requires_a_kernel_change(self):
        with self.assertRaises(ValueError) as caught:
            self.run_inspect('same', 'same', activation=True)
        self.assertIn('already', str(caught.exception))
        state = self.run_inspect('new', 'old', activation=True)
        self.assertEqual((state['image_activation'], state['selected']['notes_sha256'], state['before']['cp']['notes_sha256']),
                         (True, 'new', 'old'))


class RebootFlag(unittest.TestCase):
    def test_the_flag_names_the_identity_being_replaced(self):
        import json
        import tempfile
        from pathlib import Path
        flags = Path(tempfile.mkdtemp())
        path = owner.write_reboot_flag('dp', observe('dp', 'notes', boot='boot-1'), {'notes_sha256': 'notes'}, flags=flags)
        flag = json.loads(path.read_text())
        self.assertEqual(path, flags / 'dp.json')
        self.assertEqual((flag['plane'], flag['boot_id_before'], flag['notes_sha256'], flag['by']),
                         ('dp', 'boot-1', 'notes', 'plane_restart_owner'))
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')


if __name__ == '__main__':
    unittest.main()
