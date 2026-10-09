import unittest
from unittest.mock import AsyncMock,patch
from pathlib import Path
import tempfile
from daemon_backend import execute,require_front_mode

class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_dhcp_apply_is_revision_fenced_and_forwards_the_intent_to_the_dataplane(self):
        backend=AsyncMock()
        intent=dict(revision=4,servers={'ae1.69':dict(interface='ae1.69',address='10.1.0.2/22',pools=[['10.1.0.100','10.1.0.199']],reserved={},lease=86400,probe=False,options={})},configuration='c'*64)
        backend.run.side_effect=[dict(config=dict(revision=4,servers={}),boot_id='b',running=None),dict(config=dict(revision=5,servers=intent['servers']),boot_id='b',running=None)]
        result=await execute('dhcp','apply',intent,backend)
        self.assertEqual(result['config']['revision'],5)
        self.assertEqual(backend.run.await_args_list[-1].args,('dhcp','apply',intent))
        backend.reset_mock();backend.run.side_effect=[dict(config=dict(revision=9,servers={}))]
        with self.assertRaisesRegex(ValueError,'revision conflict'): await execute('dhcp','apply',intent,backend)
        backend.reset_mock();backend.run.side_effect=[dict(config=dict(revision=4,servers={})),dict(validated=True)]
        self.assertTrue((await execute('dhcp','validate',intent,backend))['validated'])
        self.assertEqual(backend.run.await_args_list[-1].args,('dhcp','validate',intent))
        with self.assertRaisesRegex(ValueError,'unsupported fields'): await execute('dhcp','apply',dict(intent,extra=1),backend)
        backend.reset_mock();backend.run.side_effect=None;backend.run.return_value=dict(config=dict(revision=4,servers={}))
        self.assertEqual((await execute('dhcp','status',{},backend))['config']['revision'],4)

    async def test_observation_refresh_is_backend_owned_and_revision_fenced(self):
        backend=AsyncMock()
        backend.run.side_effect=[dict(config=dict(revision=9),boot_id='new-boot'),dict(ports=[
            dict(port=1,available=True,enabled=True,link=True),dict(port=5,available=True,enabled=False,link=True)]),dict(acknowledged=True)]
        self.assertTrue((await execute('route-links','refresh',{},backend))['acknowledged'])
        self.assertEqual(backend.run.await_args_list[-1].args,('network','health',dict(revision=9,boot_id='new-boot',links=dict(p1=True,p5=False))))
        backend.reset_mock()
        with self.assertRaises(ValueError): await execute('route-links','refresh',{'links':{'p5':True}},backend)
        backend.run.assert_not_awaited()
        backend.run.side_effect=[dict(config=dict(revision=9),boot_id='old-boot'),dict(ports=[]),ValueError('revision changed')]
        with self.assertRaisesRegex(ValueError,'revision changed'): await execute('route-links','refresh',{},backend)

    async def test_route_link_refresh_validates_without_changing_hardware(self):
        backend=AsyncMock();backend.run.return_value=dict(config=dict(revision=7),boot_id='boot')
        result=await execute('network','validate',dict(revision=7,refresh_route_links=True),backend)
        self.assertTrue(result['validated'])
        backend.run.assert_awaited_once_with('network','status')

    async def test_route_link_refresh_uses_observed_hardware_not_caller_links(self):
        backend=AsyncMock()
        backend.run.side_effect=[dict(config=dict(revision=7),boot_id='boot'),dict(ports=[
            dict(port=1,available=True,enabled=True,link=True),dict(port=5,available=True,enabled=True,link=False)]),dict(acknowledged=True)]
        self.assertTrue((await execute('network','apply',dict(revision=7,refresh_route_links=True),backend))['acknowledged'])
        self.assertEqual(backend.run.await_args_list[-1].args,('network','health',dict(revision=7,boot_id='boot',links=dict(p1=True,p5=False))))
        with self.assertRaises(ValueError):
            await execute('network','apply',dict(revision=7,refresh_route_links=True,links=dict(p5=True)),backend)

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
