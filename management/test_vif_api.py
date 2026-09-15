import sys
import types
import unittest
from unittest.mock import patch
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from vif_api import router


class Api(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.user={'username':'test','role':'admin'}
        async def current():return self.user
        def admin(user):
            if user['role']!='admin':raise HTTPException(403,'admin required')
        async def audit(*args):pass
        async def rpc(path,data):
            self.calls.append(data)
            return {'ok':True,'result':{'config':{'revision':4,'vifs':{}},'ports':[5,13]}}
        self.mock=patch.dict(sys.modules,{'ffn_plane_api':types.SimpleNamespace(rpc=rpc)});self.mock.start()
        app=FastAPI();app.include_router(router(current,admin,audit),prefix='/api/system/runtime')
        self.client=TestClient(app)
    def tearDown(self):self.mock.stop()
    def test_assignment_and_status(self):
        self.assertEqual(self.client.get('/api/system/runtime/vifs').status_code,200)
        r=self.client.post('/api/system/runtime/vifs/set',json={'revision':3,'vifs':{}})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.calls[-1]['payload'],{'revision':3,'vifs':{},'operation':'set'})
    def test_readonly_and_invalid_requests_never_mutate(self):
        self.user['role']='viewer'
        self.assertEqual(self.client.post('/api/system/runtime/vifs/start',json={'revision':3}).status_code,403)
        self.user['role']='admin'
        for data in ({'revision':True},{'revision':-1},{'revision':3,'shell':'x'}):
            self.assertEqual(self.client.post('/api/system/runtime/vifs/start',json=data).status_code,422)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
