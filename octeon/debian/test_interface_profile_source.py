import json
from pathlib import Path
import tempfile
import unittest
import ffn_interface_profile_source as source


class Profiles(unittest.TestCase):
    def test_only_applied_boot_current_aggregate_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'network.json'
            policy=dict(profile='admin',ping=True,tcp=[443],udp=[],sources=[])
            config.write_text(json.dumps(dict(ports={'p1':dict(mode='l3',addresses=['192.0.2.1/24'],management=policy)})))
            unit=dict(name='ae7.42',addresses=['198.18.1.1/24'],management=policy)
            state=dict(boot_id='boot',configuration_ready=True,network_update_pending=False,
                       updated_monotonic=100,group='ae7',token='token',configuration_revision='revision',
                       network=dict(enabled=False,units=[unit]))
            status=root/'ffn-aggregate-ae7-status.json'
            (root/'ffn-aggregate-ae7-intent.json').write_text(json.dumps(dict(token='token',network_generation='revision')))
            for change in ({},dict(boot_id='old'),dict(configuration_revision='old'),
                           dict(updated_monotonic=0),dict(network_update_pending=True)):
                status.write_text(json.dumps(dict(state,**change)))
                rows=source.records(config,root,'boot',101)
                self.assertEqual([r['interface'] for r in rows],['p1'] if change else ['p1','ae7.42'])
            state.pop('configuration_ready');state['network_ready']=True
            status.write_text(json.dumps(state))
            self.assertEqual([r['interface'] for r in source.records(config,root,'boot',101)],['p1','ae7.42'])
            state['network_ready']=False;status.write_text(json.dumps(state))
            self.assertEqual([r['interface'] for r in source.records(config,root,'boot',101)],['p1'])


if __name__=='__main__':unittest.main()
