import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
from control import router
from hardware_backend import Controller


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
        for path in ('network/patch','overlay/set','inspection/set','thermal/full','lacp/set','lacp/activate','lacp/deactivate'):
            self.assertEqual(self.client.post('/api/pa5200/'+path, json={'revision':7}).status_code,403)
        self.controller.run.assert_not_called()

    def test_unknown_action_no_dispatch(self):
        self.assertEqual(self.client.post('/api/pa5200/thermal/reboot', json={}).status_code,404)
        self.controller.run.assert_not_called()

    def test_lacp_profile_and_activation_are_separate(self):
        data = {'revision':7,'groups':{}}
        self.assertEqual(self.client.post('/api/pa5200/lacp/set',json=data).status_code,200)
        self.controller.run.assert_awaited_once_with('lacp','set',data)
        self.controller.run.reset_mock()
        data = {'revision':7,'group':'lag1'}
        self.assertEqual(self.client.post('/api/pa5200/lacp/activate',json=data).status_code,200)
        self.controller.run.assert_awaited_once_with('lacp','activate',data)

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

    def test_port_observations_read_only(self):
        self.role = 'read-only'
        self.assertEqual(self.client.get('/api/pa5200/port-events').status_code, 200)
        self.controller.run.assert_awaited_once_with('port-events', 'status')
        self.controller.run.reset_mock()
        self.role = 'admin'
        self.assertEqual(self.client.post('/api/pa5200/port-events/set', json={}).status_code, 404)
        self.controller.run.assert_not_called()


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_port_events_fixed_read_adapter(self):
        from daemon_backend import execute
        from hardware_backend import COMMANDS
        backend = AsyncMock()
        await execute('port-events', 'status', {}, backend)
        backend.run.assert_awaited_once_with('port-events', 'status', None)
        self.assertEqual(COMMANDS['port-events', 'status'],
            ('/usr/local/sbin/ffn-cp', 'python3 /usr/local/sbin/ffn_port_events.py status'))
        for action, payload in [('status', {'command':'anything'}), ('apply', {}), ('validate', {})]:
            with self.assertRaises(ValueError): await execute('port-events', action, payload, backend)
    async def test_daemon_lacp_save_is_not_activation(self):
        from daemon_backend import execute
        backend=AsyncMock()
        backend.run.return_value={'config':{'revision':0,'groups':{}},'capabilities':{'activation_supported':False}}
        await execute('lacp','apply',{'revision':0,'groups':{},'operation':'set'},backend)
        self.assertEqual(backend.run.call_args.args,('lacp','set',{'revision':0,'groups':{}}))

    async def test_daemon_lacp_activation_rejects_unqualified_backend(self):
        from daemon_backend import execute
        backend=AsyncMock()
        backend.run.return_value={'config':{'revision':0,'groups':{}},'capabilities':{'activation_supported':False}}
        with self.assertRaises(ValueError):
            await execute('lacp','validate',{'revision':0,'group':'lag1','operation':'activate'},backend)
        backend.run.assert_awaited_once_with('lacp','status')

    async def test_unknown_command_never_spawns(self):
        with patch('hardware_backend.asyncio.create_subprocess_exec') as spawn:
            with self.assertRaises(HTTPException):
                await Controller().run('network; reboot','status')
            spawn.assert_not_called()

    async def test_missing_binary_no_spawn(self):
        with patch('hardware_backend.os.access',return_value=False), patch('hardware_backend.asyncio.create_subprocess_exec') as spawn:
            with self.assertRaises(HTTPException) as e:
                await Controller().run('network','status')
            self.assertEqual(e.exception.status_code,503)
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
