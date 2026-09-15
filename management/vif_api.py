"""Authenticated optional VIF controls through the MP control daemon."""
import asyncio
import json
import os
import uuid
from fastapi import APIRouter,Depends,HTTPException,Request


def router(current_user,require_admin,record_audit):
    api=APIRouter()
    async def call(action,payload):
        from ffn_plane_api import rpc
        request={'v':1,'id':str(uuid.uuid4()),'resource':'vifs','action':action,'payload':payload}
        try:reply=await rpc(os.environ.get('FFN_PLANE_SOCKET','/run/ffn-plane-mp/control.sock'),request)
        except (OSError,ValueError,asyncio.TimeoutError):raise HTTPException(503,'VIF outcome unknown; refresh status')
        if not reply.get('ok'):
            detail=str(reply.get('error','VIF operation rejected'))
            if reply.get('state')=='unknown':detail+='; request ID '+request['id']+' requires MP journal reconciliation'
            raise HTTPException(409,detail)
        return reply['result']
    @api.get('/vifs')
    async def status(user=Depends(current_user)):return await call('status',{})
    @api.post('/vifs/{operation}')
    async def change(operation:str,request:Request,user=Depends(current_user)):
        require_admin(user)
        if operation not in ('set','start','stop','recover'):raise HTTPException(404,'Unknown VIF operation')
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>65536:raise HTTPException(413,'VIF request too large')
        try:
            data=json.loads(raw)
            expected={'revision','vifs'} if operation=='set' else {'revision'}
            if not isinstance(data,dict) or set(data)!=expected or type(data['revision']) is not int or data['revision']<0:raise ValueError()
        except (ValueError,UnicodeError):raise HTTPException(422,'Invalid VIF configuration')
        await record_audit(user['username'],'vif_request',operation+' revision='+str(data['revision']))
        result=await call('apply',data|{'operation':operation})
        await record_audit(user['username'],'vif_completed',operation+' revision='+str(result['config']['revision']))
        return result
    return api
