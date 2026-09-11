import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
from control import Controller, router, inspection_activation


class APITests(unittest.TestCase):
    def setUp(self):
        self.role = 'admin'
        async def current():
            if self.role is None:
                raise HTTPException(401)
            return {'username':'test', 'role':self.role}
        def admin(user):
            if user['role'] not in ('admin', 'superuser'):
                raise HTTPException(403)
        self.controller = AsyncMock()
        self.controller.run.return_value = {'config': {'revision': 7}}
        self.audit = AsyncMock()
        self.app = FastAPI()
        self.app.include_router(router(current, admin, self.audit, self.controller))
        self.client = TestClient(self.app)

    def test_no_auth_no_controller(self):
        self.role = None
        for url in ('/api/pa5200/status', '/api/pa5200/network'):
            self.assertEqual(self.client.get(url).status_code, 401)
        self.controller.run.assert_not_called()

    def test_readonly_cannot_write(self):
        self.role = 'read-only'
        for path in ('network/patch','overlay/set','inspection/set','thermal/full'):
            self.assertEqual(self.client.post('/api/pa5200/'+path, json={'revision':7}).status_code,403)
        self.controller.run.assert_not_called()

    def test_unknown_action_no_dispatch(self):
        self.assertEqual(self.client.post('/api/pa5200/thermal/reboot', json={}).status_code,404)
        self.controller.run.assert_not_called()

    def test_revision_required_and_unknown_fields_rejected(self):
        for data in ({}, {'revision':True}, {'revision':-1}, {'revision':7,'command':'reboot'}):
            self.assertEqual(self.client.post('/api/pa5200/network/patch',json=data).status_code,422)
        self.controller.run.assert_not_called()

    def test_payload_limits_and_nonfinite(self):
        for data,code in [('x'*65537,413), ('[]',422), ('{"revision":NaN}',422)]:
            self.assertEqual(self.client.post('/api/pa5200/network/patch',content=data).status_code,code)

    def test_patch_preserves_payload_and_redacts_audit(self):
        data={'revision':7,'mode':'alert','ports':[1], 'literal':'private inspection literal'}
        self.assertEqual(self.client.post('/api/pa5200/inspection/set',json=data).status_code,200)
        self.controller.run.assert_awaited_once_with('inspection','set',data)
        self.assertNotIn(data['literal'],str(self.audit.call_args_list))

    def test_conflict_propagates_and_audits(self):
        self.controller.run.side_effect=HTTPException(409,'revision conflict')
        self.assertEqual(self.client.post('/api/pa5200/network/patch',json={'revision':7}).status_code,409)
        self.assertEqual(self.audit.call_args.args[1], 'pa5200_failed')

    def test_partial_status_retains_errors(self):
        async def run(resource, action):
            if resource == 'network': raise HTTPException(503,'unreachable')
            return {'healthy':True}
        self.controller.run.side_effect=run
        result=self.client.get('/api/pa5200/status').json()
        self.assertFalse(result['resources']['network']['available'])
        self.assertTrue(result['resources']['thermal']['available'])
        self.assertFalse(result['capabilities']['hardware_flow_offload'])

    def test_readonly_route_lookup(self):
        self.role='read-only'
        self.assertEqual(self.client.post('/api/pa5200/network/lookup',json={'dst':'198.18.1.2','vrf':'vrf-blue'}).status_code,200)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_plane_socket_uses_rpc_without_legacy_command(self):
        import types
        rpc=AsyncMock(return_value={'ok':True,'state':'applied','result':{'config':{'revision':8}},'trace':['mp','cp','dp']})
        with patch.dict('os.environ',{'FFN_PLANE_SOCKET':'/run/fixture.sock'}), \
             patch.dict('sys.modules',{'ffn_plane_api':types.SimpleNamespace(rpc=rpc)}), \
             patch('control.asyncio.create_subprocess_exec') as spawn:
            result=await Controller().run('network','patch',{'revision':7,'ports':{}})
        self.assertEqual(result['control']['trace'],['mp','cp','dp'])
        self.assertEqual(rpc.call_args.args[1]['action'],'apply')
        spawn.assert_not_called()

    async def test_inspection_activation_requires_live_matching_revision(self):
        ctl=AsyncMock()
        result={'accepted':{'revision':8}}
        for observed,expected in [
            ({'config':{'revision':8},'running':True,'runtime':{'revision':8}},'active'),
            ({'config':{'revision':8},'running':False,'runtime':{'revision':8}},'pending'),
            ({'config':{'revision':8},'running':True,'runtime':{'revision':7}},'pending'),
            ({'config':{'revision':9},'running':True,'runtime':{'revision':9}},'superseded'),
            ({'config':{'revision':8},'runtime':{'reload_error':'private diagnostic'}},'failed')]:
            ctl.run.return_value=observed
            answer=await inspection_activation(ctl,result,attempts=1)
            self.assertEqual(answer['activation'],expected)
            self.assertNotIn('private diagnostic',str(answer))
        ctl.run.side_effect=HTTPException(503)
        self.assertEqual((await inspection_activation(ctl,result))['activation'],'unknown')

    async def test_unknown_command_never_spawns(self):
        with patch('control.asyncio.create_subprocess_exec') as spawn:
            with self.assertRaises(HTTPException):
                await Controller().run('network; reboot','status')
            spawn.assert_not_called()

    async def test_missing_binary_no_spawn(self):
        with patch('control.os.access',return_value=False), patch('control.asyncio.create_subprocess_exec') as spawn:
            with self.assertRaises(HTTPException) as e:
                await Controller().run('network','status')
            self.assertEqual(e.exception.status_code,503)
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
