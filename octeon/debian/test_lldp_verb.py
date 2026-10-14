"""The dataplane LLDP plane verb: intent validation, rendering with frame addresses, apply and status."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffn_lldp as verb

INTENT = dict(enabled=True, system=dict(chassis_id='08:66:1f:03:08:cd', name='PA-5220-FFN', description='FFN-NGFW PA-5200'), interval=30, hold=4,
              ports=[dict(port=5, mode='transmit-receive', tlvs=['system-name'], description='WAN', management=['152.193.185.10'], aggregate=''),
                     dict(port=21, mode='transmit-only', tlvs=list(verb.TLVS), description='', management=[], aggregate='ae1')])


class FakeRun:
    def __init__(self, links):
        self.links = links; self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        class Result: pass
        r = Result(); r.returncode = 0; r.stdout = json.dumps([dict(ifname=k, address=v) for k, v in self.links.items()]); r.stderr = ''
        return r


class LldpVerbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.patches = [patch.object(verb, 'INTENT', root / 'etc/lldp.json'), patch.object(verb, 'RUN', root / 'run')]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        (root / 'run').mkdir()

    def test_validation_normalises_and_rejects(self):
        clean = verb.validate_intent(INTENT)
        self.assertEqual([p['port'] for p in clean['ports']], [5, 21])
        self.assertEqual(clean['ports'][1]['aggregate'], 'ae1')
        for broken in (dict(INTENT, enabled='yes'), dict(INTENT, hold=1), dict(INTENT, system=dict(chassis_id='08:66:1F:03:08:CD')),
                       dict(INTENT, ports=INTENT['ports'] + [dict(port=5)]), dict(INTENT, ports=[dict(port=99)]),
                       dict(INTENT, ports=[dict(port=5, mode='loud')]), dict(INTENT, ports=[dict(port=5, tlvs=['vendor'])]),
                       dict(INTENT, ports=[dict(port=5, aggregate='bond0')])):
            with self.assertRaises(ValueError): verb.validate_intent(broken)

    def test_render_uses_the_port_or_aggregate_address(self):
        doc = verb.render(verb.validate_intent(INTENT), 'acc0e09a', {'p5': 'b2:bd:5a:dd:09:6a', 'ae1': '02:8b:77:fb:05:f9'})
        self.assertEqual([(p['name'], p['source'], p['mac']) for p in doc['ports']], [('ethernet1/5', 16, 'b2:bd:5a:dd:09:6a'), ('ethernet1/21', 32, '02:8b:77:fb:05:f9')])
        self.assertEqual(doc['ports'][0]['management'], ['152.193.185.10']); self.assertEqual(doc['configuration'], 'acc0e09a')
        with self.assertRaisesRegex(ValueError, 'ethernet1/21'): verb.render(verb.validate_intent(INTENT), 'x', {'p5': 'b2:bd:5a:dd:09:6a'})
        off = verb.render(verb.validate_intent(dict(INTENT, enabled=False)), 'x', {})
        self.assertEqual(off['ports'], [])

    def test_apply_writes_the_agent_document_and_reloads(self):
        run = FakeRun({'p5': 'b2:bd:5a:dd:09:6a', 'ae1': '02:8b:77:fb:05:f9'})
        before = verb.status()
        self.assertEqual(before['config']['revision'], 0)
        with self.assertRaisesRegex(ValueError, 'revision'): verb.execute('apply', dict(revision=1, lldp=INTENT, configuration='acc0e09a'), run)
        self.assertEqual(verb.execute('validate', dict(revision=0, lldp=INTENT, configuration='acc0e09a'), run), dict(validated=True, ports=2))
        self.assertFalse(verb.INTENT.exists())
        result = verb.execute('apply', dict(revision=0, lldp=INTENT, configuration='acc0e09a'), run)
        self.assertTrue(result['applied'])
        self.assertEqual(run.calls[-1], ['systemctl', 'reload-or-restart', 'ffn-lldpd.service'])
        saved = json.loads(verb.INTENT.read_text())
        self.assertEqual(saved['configuration'], 'acc0e09a'); self.assertEqual(saved['system']['chassis_id'], '08:66:1f:03:08:cd')
        self.assertEqual(saved['ports'][0]['source'], 16); self.assertEqual(saved['intent']['enabled'], True)
        after = verb.status()
        self.assertEqual(after['config']['lldp'], verb.validate_intent(INTENT)); self.assertNotEqual(after['config']['revision'], 0)
        self.assertIsNone(after['agent'])
        (verb.RUN / 'status.json').write_text(json.dumps({'running': True, 'ports': []}))
        self.assertTrue(verb.status()['agent']['running'])
        # a stale revision never applies
        with self.assertRaises(ValueError): verb.execute('apply', dict(revision=0, lldp=INTENT, configuration='acc0e09a'), run)
        with self.assertRaises(ValueError): verb.execute('status', {'x': 1}, run)


if __name__ == '__main__':
    unittest.main()
