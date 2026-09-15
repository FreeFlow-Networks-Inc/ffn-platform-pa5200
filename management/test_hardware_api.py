import unittest
from unittest.mock import AsyncMock
from fastapi import FastAPI,HTTPException
import httpx
from hardware_harness import router


class HardwareAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.role='viewer'
        async def user(): return {'username':'test','role':self.role}
        def admin(user):
            if user['role']!='admin': raise HTTPException(403)
        self.harness=AsyncMock()
        self.harness.status.return_value={'session_offload_available':False}
        self.harness.prepare.return_value={'changed':False}
        self.audit=AsyncMock()
        app=FastAPI(); app.include_router(router(self.harness,user,admin,self.audit))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
    async def test_status(self):
        self.assertEqual((await self.client.get('/api/pa5200/hardware/status')).status_code,200)
    async def test_viewer_cannot_prepare(self):
        result=await self.client.post('/api/pa5200/hardware/prepare',json={'operation':'prepare-dma','boot_id':'a'})
        self.assertEqual(result.status_code,403); self.harness.prepare.assert_not_called()
    async def test_admin_and_extra_fields(self):
        self.role='admin'
        payload={'operation':'prepare-dma','boot_id':'a'}
        self.assertEqual((await self.client.post('/api/pa5200/hardware/prepare',json=payload)).status_code,200)
        self.audit.assert_awaited_once()
        self.assertEqual((await self.client.post('/api/pa5200/hardware/prepare',json=dict(payload,register=1))).status_code,422)

if __name__=='__main__': unittest.main()
