import unittest
from unittest.mock import AsyncMock,patch
from pathlib import Path
import tempfile
from daemon_backend import execute,require_front_mode

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_cannot_be_bypassed_by_direct_hardware_enable(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'running.xml'
            def config(content):path.write_text('<config><devices><entry name="localhost.localdomain"><network><interface><ethernet>'+content+'</ethernet></interface></network></entry></devices></config>')
            for entry in ('','<entry name="ethernet1/2"/>','<entry name="ethernet1/2"><aggregate-group/></entry>','<entry name="ethernet1/2"><layer3/><link-state>down</link-state></entry>'):
                config(entry)
                with self.assertRaisesRegex(ValueError,'None/disabled'):require_front_mode(2,path)
            config('<entry name="ethernet1/2"><layer3/></entry>')
            require_front_mode(2,path)
        backend=AsyncMock();backend.run.return_value={'revision':7,'ports':[{'port':2}]}
        with patch('daemon_backend.require_front_mode',side_effect=ValueError('None/disabled')):
            with self.assertRaisesRegex(ValueError,'None/disabled'):
                await execute('faceplate','apply',{'revision':7,'port':2,'enabled':True},backend)
        backend.run.assert_awaited_once_with('faceplate','status')
    async def test_network_validation_checks_physical_attachment_before_apply(self):
        backend=AsyncMock()
        backend.run.side_effect=[{'config':{'revision':7}},ValueError('physical port unattached')]
        payload={'revision':7,'ports':{'p5':{'mode':'l3','addresses':[]}}}
        with self.assertRaisesRegex(ValueError,'unattached'):
            await execute('network','validate',payload,backend)
        self.assertEqual([c.args for c in backend.run.await_args_list],
                         [('network','status'),('network','validate',payload)])

    async def test_pair_recovery_requires_down_port_capability_and_standalone_request(self):
        backend=AsyncMock();row={'port':4,'media':'copper','enabled':True,'pair_map_recovery':True}
        backend.run.return_value={'revision':8,'ports':[row]}
        payload={'revision':8,'port':4,'restore_pair_map':True}
        self.assertEqual(await execute('faceplate','validate',payload,backend),{'validated':True})
        backend.run.assert_awaited_once_with('faceplate','status')
        for changes in ({'enabled':False},{'pair_map_recovery':False},{'phy_pending':True}):
            backend.run.return_value={'revision':8,'ports':[row|changes]}
            with self.assertRaises(ValueError):await execute('faceplate','apply',payload,backend)
        backend.run.return_value={'revision':8,'ports':[row]}
        for changes in ({'restore_pair_map':1},{'speed':'auto'},{'restart_autoneg':True}):
            with self.assertRaises(ValueError):await execute('faceplate','apply',payload|changes,backend)

    async def test_renegotiation_is_copper_only_and_validation_is_read_only(self):
        backend=AsyncMock();row={'port':3,'enabled':True,'media':'copper','renegotiate_configuration':True}
        backend.run.return_value={'revision':8,'ports':[row]}
        payload={'revision':8,'port':3,'restart_autoneg':True}
        self.assertEqual(await execute('faceplate','validate',payload,backend),{'validated':True})
        backend.run.assert_awaited_once_with('faceplate','status')
        for changes in ({'enabled':False},{'phy_pending':True},{'renegotiate_configuration':False}):
            backend.run.return_value={'revision':8,'ports':[row|changes]}
            with self.assertRaises(ValueError):await execute('faceplate','apply',payload,backend)
        backend.run.return_value={'revision':8,'ports':[row]}
        for fields in ({'restart_autoneg':1},{'speed':'auto'},{'port':5}):
            with self.assertRaises(ValueError):await execute('faceplate','apply',payload|fields,backend)

    async def test_validation_does_not_apply(self):
        backend=AsyncMock();backend.run.return_value={'revision':7}
        payload={'revision':7,'port':1,'enabled':False}
        self.assertEqual(await execute('faceplate','validate',payload,backend),{'validated':True})
        backend.run.assert_awaited_once_with('faceplate','status')
        backend.run.reset_mock()
        await execute('faceplate','apply',payload,backend)
        self.assertEqual(backend.run.await_args.args,('faceplate','set',payload))

    async def test_stale_and_raw_requests_rejected(self):
        backend=AsyncMock();backend.run.return_value={'revision':8}
        for payload in ({'revision':7,'port':1,'enabled':True},{'revision':8,'shell':'reboot'}):
            with self.assertRaises(ValueError): await execute('faceplate','apply',payload,backend)
        self.assertFalse(any(c.args[1]=='set' for c in backend.run.await_args_list))

if __name__=='__main__': unittest.main()
