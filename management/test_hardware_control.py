import unittest
import uuid
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from hardware_control import execute
from daemon_backend import execute as execute_resource


class Control(unittest.IsolatedAsyncioTestCase):
    def test_web_commit_barrier_uses_gateway_and_exact_digest(self):
        from control import before_policy_commit
        client=Mock();client.plane_request.side_effect=[{'ok':True,'result':{'revision':7}},
            {'ok':True,'result':{'revision':8,'phase':'blocked','sessions':0,'recovery_required':False}}]
        with patch.dict(os.environ,{'FFN_CONTROL_GATEWAY':'controld'}), \
                patch.dict('sys.modules',{'ffn_controld_client':SimpleNamespace(ControldClient=Mock(return_value=client))}):
            result=before_policy_commit(b'<config/>')
        self.assertTrue(result['drained'])
        first,second=[call.args[0] for call in client.plane_request.call_args_list]
        self.assertEqual(first['resource'],'fe100-policy');self.assertEqual(first['action'],'status')
        self.assertEqual(second['payload'],{'revision':7,'digest':hashlib.sha256(b'<config/>').hexdigest()})
        self.assertNotEqual(first['id'],second['id'])

    async def test_validate_checks_boot_without_preparing(self):
        boot=str(uuid.uuid4());harness=AsyncMock()
        harness.status.return_value={'dp':{'available':True,'data':{'boot_id':boot}}}
        payload={'revision':0,'operation':'prepare-dma','expected_boot_id':boot}
        self.assertEqual(await execute('validate',payload,harness),{'validated':True})
        harness.prepare.assert_not_called()
        with self.assertRaises(ValueError):
            await execute('validate',payload|{'expected_boot_id':str(uuid.uuid4())},harness)
        await execute('apply',payload,harness)
        harness.prepare.assert_awaited_once_with('prepare-dma',boot)

    async def test_arbitrary_preparation_is_rejected(self):
        harness=AsyncMock()
        for payload in ({}, {'revision':0,'operation':'shell','expected_boot_id':str(uuid.uuid4())}):
            with self.assertRaises(ValueError):await execute('apply',payload,harness)
        harness.prepare.assert_not_called()

    async def test_fe100_policy_accepts_digest_only_at_current_revision(self):
        backend=AsyncMock();backend.run.return_value={'revision':8}
        payload={'revision':8,'digest':'a'*64}
        self.assertEqual(await execute_resource('fe100-policy','validate',payload,backend),{'validated':True})
        backend.run.assert_awaited_once_with('fe100-policy','status')
        for fields in ({'digest':'bad'},{'revision':7},{'admission':True}):
            with self.assertRaises(ValueError):await execute_resource('fe100-policy','apply',payload|fields,backend)
        await execute_resource('fe100-policy','apply',payload,backend)
        self.assertEqual(backend.run.await_args.args,('fe100-policy','set',payload))


if __name__=='__main__':unittest.main()
