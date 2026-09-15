import unittest
from unittest.mock import AsyncMock
from fastapi import FastAPI, HTTPException
import httpx
from hardware_harness import router


class RuntimeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness=AsyncMock()
        self.audit=AsyncMock()
        self.admin=True
        async def user(): return {'username':'operator'}
        def require_admin(_):
            if not self.admin: raise HTTPException(403,'Administrator required')
        app=FastAPI()
        app.include_router(router(self.harness,user,require_admin,self.audit))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_runtime_requires_admin(self):
        self.admin=False
        result=await self.client.post('/api/pa5200/hardware/runtime',json={
            'operation':'start-trunk','boot_id':'boot-a'})
        self.assertEqual(result.status_code,403)
        self.harness.runtime.assert_not_called()

    async def test_runtime_forwards_boot_id_and_audits(self):
        self.harness.runtime.return_value={'verified':True,'changed':True}
        result=await self.client.post('/api/pa5200/hardware/runtime',json={
            'operation':'stop-trunk','boot_id':'boot-a'})
        self.assertEqual(result.status_code,200)
        self.harness.runtime.assert_awaited_once_with('stop-trunk','boot-a')
        self.audit.assert_awaited_once_with('operator','hardware_runtime','stop-trunk')

    async def test_runtime_errors_are_sanitized_and_not_retried(self):
        self.harness.runtime.side_effect=RuntimeError('private connection details')
        result=await self.client.post('/api/pa5200/hardware/runtime',json={
            'operation':'start-trunk','boot_id':'boot-a'})
        self.assertEqual(result.status_code,409)
        self.assertNotIn('private',result.text)
        self.harness.runtime.assert_awaited_once()

    async def test_runtime_requires_boot_id_and_rejects_extra_commands(self):
        for payload in ({'operation':'start-trunk'},
                        {'operation':'start-trunk','boot_id':'boot-a','command':'anything'}):
            result=await self.client.post('/api/pa5200/hardware/runtime',json=payload)
            self.assertEqual(result.status_code,422)
        self.harness.runtime.assert_not_called()


if __name__=='__main__': unittest.main()
