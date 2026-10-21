"""The MP route-link feed comes back with the control daemon it is part of.

2026-10-20: a core patch install stopped ffn-controld; PartOf= propagated the stop to
ffn-route-link-feed.service, nothing started it again (Restart= does not apply to an
explicit stop), the dataplane's link observation went stale after 60 s and the route
tracker withdrew both default routes for fifteen minutes. The unit now declares itself
wanted by the daemon, and the installer turns every WantedBy= target into the wants link
systemd needs for the declaration to mean anything -- the session feed and the
interface-service tunnel had declared the same for months with only the multi-user
link ever made."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
CONTROL_DAEMON = 'ffn-controld.service'


def unit(name):
    return (HERE / name).read_text()


def sections(text):
    """{section: {key: [values]}} of a unit file; a repeated key appends."""
    found, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        if line.startswith('['):
            current = found.setdefault(line.strip('[]').strip(), {})
            continue
        key, _, value = line.partition('=')
        current.setdefault(key.strip(), []).append(value.strip())
    return found


def can_symlink():
    try:
        with tempfile.TemporaryDirectory() as temp:
            os.symlink('target', os.path.join(temp, 'link'))
    except (OSError, NotImplementedError):
        return False
    return True


def installer():
    spec = importlib.util.spec_from_file_location('install_control_channel', HERE / 'install-control-channel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeedUnit(unittest.TestCase):
    def test_the_feed_is_part_of_and_wanted_by_the_control_daemon(self):
        found = sections(unit('ffn-route-link-feed.service'))
        self.assertEqual(found['Unit']['PartOf'], [CONTROL_DAEMON])
        self.assertEqual(found['Unit']['Requires'], [CONTROL_DAEMON])
        self.assertEqual(found['Unit']['After'], [CONTROL_DAEMON])
        self.assertEqual(found['Unit']['StartLimitIntervalSec'], ['0'])
        self.assertEqual(found['Service']['Restart'], ['always'])
        wanted = ' '.join(found['Install']['WantedBy']).split()
        self.assertIn(CONTROL_DAEMON, wanted, 'the daemon must pull the feed back in when it starts')
        self.assertIn('multi-user.target', wanted)

    def test_every_dependent_of_the_daemon_declares_the_same_recovery(self):
        for name in ('ffn-fe100-session-feed.service', 'ffn-interface-service-tunnel.service', 'ffn-route-link-feed.service'):
            with self.subTest(unit=name):
                found = sections(unit(name))
                self.assertEqual(found['Unit']['PartOf'], [CONTROL_DAEMON])
                self.assertIn(CONTROL_DAEMON, ' '.join(found['Install']['WantedBy']).split())

    @unittest.skipUnless(can_symlink(), 'symlinks need a privilege this account lacks (Windows)')
    def test_the_installer_makes_a_wants_link_for_every_wanted_by_target(self):
        module = installer()
        text = unit('ffn-route-link-feed.service')
        self.assertEqual(module.wanted_by(text), ['multi-user.target', CONTROL_DAEMON])
        with tempfile.TemporaryDirectory() as temp:
            system = Path(temp)
            made = module.enable_unit('ffn-route-link-feed.service', text, system)
            links = [system / 'multi-user.target.wants' / 'ffn-route-link-feed.service',
                     system / 'ffn-controld.service.wants' / 'ffn-route-link-feed.service']
            self.assertEqual(made, [str(link) for link in links])
            for link in links:
                self.assertTrue(link.is_symlink(), link)
                self.assertEqual(link.readlink(), Path('../ffn-route-link-feed.service'))
            # Idempotent: a second run touches nothing and reports nothing made.
            self.assertEqual(module.enable_unit('ffn-route-link-feed.service', text, system), [])
            # A link the operator pointed elsewhere is left alone, not replaced.
            other = system / 'multi-user.target.wants' / 'ffn-fe100-session-feed.service'
            other.symlink_to('/etc/systemd/system/ffn-fe100-session-feed.service')
            self.assertEqual(module.enable_unit('ffn-fe100-session-feed.service', unit('ffn-fe100-session-feed.service'), system),
                             [str(system / 'ffn-controld.service.wants' / 'ffn-fe100-session-feed.service')])
            self.assertEqual(other.readlink(), Path('/etc/systemd/system/ffn-fe100-session-feed.service'))

    def test_wanted_by_reads_only_the_install_section(self):
        module = installer()
        text = ('[Unit]\nWantedBy=not-here.target\n\n[Install]\n# a comment\n; another\n'
                'WantedBy=a.target  b.service\nWantedBy=b.service c.target\nAlias=x\n')
        self.assertEqual(module.wanted_by(text), ['a.target', 'b.service', 'c.target'])
        self.assertEqual(module.wanted_by('[Service]\nExecStart=/bin/true\n'), [])

    def test_the_installer_registers_the_tracked_routes_check(self):
        # The convergence subsystem the core asks the daemon for; without this entry the
        # check is "not observable" rather than failed, so the registration is a test.
        source = (HERE / 'install-control-channel.py').read_text()
        self.assertIn("worker['commands']['tracked-routes']={'status':", source)


if __name__ == '__main__':
    unittest.main()
