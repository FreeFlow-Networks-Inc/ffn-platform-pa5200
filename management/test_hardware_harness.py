import unittest
from unittest.mock import AsyncMock,Mock
from hardware_harness import create_harness,Harness


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cp,self.dp=AsyncMock(),AsyncMock()
        self.h=Harness(self.cp,self.dp)
        self.packet={'pki_active':0,'pki_enabled':0,'pko_enabled':0,'dma_ready':True}
        self.observed={'ready':True,'boot_id':'boot-a','packet_initialization':self.packet}
        self.dp.status.return_value=self.observed
        self.cp.status.return_value={'recovery_required':True}

    def test_non_pa_has_no_hardware_calls(self):
        factory=Mock()
        self.assertIsNone(create_harness('generic',factory))
        factory.assert_not_called()

    async def test_no_repeated_preparation(self):
        result=await self.h.prepare('prepare-dma','boot-a')
        self.assertFalse(result['changed'])
        self.dp.prepare.assert_not_called()

    async def test_boot_change_rejects(self):
        with self.assertRaises(RuntimeError): await self.h.prepare('prepare-sso','boot-old')
        self.dp.prepare.assert_not_called()

    async def test_missing_status_fails_closed(self):
        self.dp.status.return_value={'ready':True,'boot_id':'boot-a'}
        with self.assertRaises(RuntimeError): await self.h.prepare('prepare-dma','boot-a')
        self.dp.prepare.assert_not_called()

    async def test_prerequisite(self):
        self.packet['dma_ready']=False
        with self.assertRaises(RuntimeError): await self.h.prepare('prepare-sso','boot-a')
        self.dp.prepare.assert_not_called()

    async def test_postcondition_required_no_retry(self):
        with self.assertRaises(RuntimeError): await self.h.prepare('prepare-sso','boot-a')
        self.dp.prepare.assert_awaited_once_with('prepare-sso')

    async def test_postcondition_verified(self):
        after=dict(self.observed,packet_initialization=dict(self.packet,sso_xaq_prepared=True))
        self.dp.status.side_effect=[self.observed,after]
        self.assertTrue((await self.h.prepare('prepare-sso','boot-a'))['verified'])

    async def test_unavailable_does_not_claim_offload(self):
        self.cp.status.side_effect=OSError('private connection details')
        result=await self.h.status()
        self.assertFalse(result['cp']['available'])
        self.assertFalse(result['session_offload_available'])
        self.assertNotIn('private',str(result))

    async def test_no_unqualified_activation(self):
        for action in ('activate','session-install','write-register','train-ddr'):
            with self.assertRaises(ValueError): await self.h.prepare(action,'boot-a')
        self.dp.prepare.assert_not_called()

    def registered(self,enabled=False):
        self.packet.update(trunk_registered=True,pki_enabled=int(enabled),pko_enabled=int(enabled),
            trunk={'interface':'ffnpkt0','running':enabled,'dq_open':enabled,'error':0})

    async def test_runtime_start_and_stop_readback(self):
        self.registered()
        for operation,enabled in [('start-trunk',True),('stop-trunk',False)]:
            async def transition(_): self.registered(enabled)
            self.dp.prepare.side_effect=transition
            self.assertTrue((await self.h.runtime(operation,'boot-a'))['verified'])
            self.assertFalse((await self.h.runtime(operation,'boot-a'))['changed'])
        self.assertEqual(self.dp.prepare.await_count,2)

    async def test_runtime_refuses_fault_and_changed_boot(self):
        self.registered()
        with self.assertRaises(RuntimeError): await self.h.runtime('start-trunk','old-boot')
        self.packet['trunk']['error']=-5
        with self.assertRaises(RuntimeError): await self.h.runtime('start-trunk','boot-a')
        self.dp.prepare.assert_not_called()

    async def test_runtime_stop_attempted_on_fault_without_retry(self):
        self.registered(True)
        self.packet['trunk']['error']=-5
        with self.assertRaises(RuntimeError): await self.h.runtime('stop-trunk','boot-a')
        self.dp.prepare.assert_awaited_once_with('stop-trunk')

    async def test_runtime_requires_engine_and_queue_postconditions(self):
        self.registered()
        async def incomplete(_): self.packet['trunk']['running']=True
        self.dp.prepare.side_effect=incomplete
        with self.assertRaises(RuntimeError): await self.h.runtime('start-trunk','boot-a')
        self.dp.prepare.assert_awaited_once_with('start-trunk')

    async def test_runtime_reboot_during_transition_not_verified(self):
        self.registered()
        async def reboot(_):
            self.registered(True)
            self.observed['boot_id']='boot-b'
        self.dp.prepare.side_effect=reboot
        with self.assertRaises(RuntimeError): await self.h.runtime('start-trunk','boot-a')


if __name__=='__main__': unittest.main()
