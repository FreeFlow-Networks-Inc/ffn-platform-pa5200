#!/usr/bin/env python3
"""Read-only check of selected extension mounting and deployed VIF controls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,'/opt/ffn-ngfw-v2')
sys.path.insert(0,'/opt/ffn-ngfw-v2/opt')
from fastapi import FastAPI
from ffn_extensions import install
from ffn_plane_api import rpc
import uuid

async def user():return {'username':'verification','role':'admin'}
def admin(value):pass
async def audit(*args):pass
root=Path('/opt/ffn-platforms/pa5200-management')
app=FastAPI();install(app,user,admin,audit,selected=str(root))
paths={r.path for r in app.routes}
for prefix in ('/api/pa5200','/api/system/runtime'):
    assert prefix+'/vifs' in paths and prefix+'/vifs/{operation}' in paths,paths
plain=FastAPI();install(plain,user,admin,audit,selected='')
assert not any('/vifs' in r.path for r in plain.routes)
manifest=json.loads((root/'extension.json').read_text())
assert any(p['id']=='vifs' for p in manifest['pages'])
request={'v':1,'id':str(uuid.uuid4()),'resource':'vifs','action':'status','payload':{}}
status=asyncio.run(rpc('/run/ffn-plane-mp/control.sock',request))
assert status.get('ok'),status
print(json.dumps({'selected_routes_present':True,'generic_platform_has_no_vif_routes':True,
                  'webui_page_declared':True,'mp_daemon_status':status['result']}))
