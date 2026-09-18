# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional PA-5200 hardware orchestration contract.

Core FFN code may construct this only through create_harness(). Adapters own
their plane's locks, persistent journals, bounded commands and live readback.
No physical addresses, arbitrary commands or owner-library pointers cross here.
"""
from typing import Protocol


class Plane(Protocol):
    async def status(self) -> dict: ...
    async def prepare(self, operation: str) -> dict: ...


DP_STAGES={
    'prepare-pki':('pki_microcode_prepared',()),
    'prepare-dma':('dma_ready',()),
    'prepare-sso':('sso_xaq_prepared',('dma_ready',)),
    'prepare-pko-memory':('pko_memory_ready',('dma_ready',)),
    'prepare-pko-queues':('pko_queues_prepared',('pko_memory_ready',)),
    'prepare-trunk':('trunk_registered',('dma_ready','sso_xaq_prepared',
                                       'pko_queues_prepared','pki_microcode_prepared')),
}
DP_RUNTIME={'start-trunk':True,'stop-trunk':False}


def create_harness(platform, adapter_factory):
    """The factory is never called on generic or unsupported hardware."""
    if platform not in ('pa5200','pa5220'): return None
    import os
    if adapter_factory is installed_adapters and os.environ.get('FFN_CONTROL_GATEWAY') == 'controld':
        return RemoteHarness()
    cp,dp=adapter_factory()
    return Harness(cp,dp)


class RemoteHarness:
    """Frontend delegates the whole operation, including its boot fence."""
    async def request(self, action, payload):
        import uuid
        from ffn_control_plane import control_rpc
        request = {'v':1, 'id':str(uuid.uuid4()), 'resource':'hardware',
                   'action':action, 'payload':payload}
        result = await control_rpc('plane/request', {'request':request})
        if not result.get('ok'):
            raise RuntimeError('Hardware control ' + result.get('state','unknown') + '; request ID ' + request['id'])
        return result['result']

    async def status(self):
        return await self.request('status', {})

    async def prepare(self, operation, expected_boot_id):
        return await self.request('apply', {'revision':0, 'operation':operation, 'expected_boot_id':expected_boot_id})

    async def runtime(self, operation, expected_boot_id):
        return await self.prepare(operation, expected_boot_id)


def installed_adapters():
    from hardware_backend import Controller
    class InstalledPlane:
        def __init__(self,resource): self.resource=resource; self.controller=Controller()
        async def status(self):
            from fastapi import HTTPException
            try: return await self.controller.run('packet-init' if self.resource=='dataplane' else self.resource,'status')
            except HTTPException: raise RuntimeError('plane controller unavailable') from None
        async def prepare(self,operation):
            if self.resource!='dataplane' or operation not in (*DP_STAGES,*DP_RUNTIME):
                raise ValueError('unsupported plane preparation')
            from fastapi import HTTPException
            try: return await self.controller.run('packet-init',operation,{})
            except HTTPException: raise RuntimeError('preparation outcome unverified') from None
    return InstalledPlane('accelerator'),InstalledPlane('dataplane')


class Harness:
    def __init__(self,cp:Plane,dp:Plane):
        import asyncio
        self.cp,self.dp=cp,dp
        self.lock=asyncio.Lock()

    async def status(self):
        async def sample(plane):
            try: return {'available':True,'data':await plane.status()}
            except (OSError,RuntimeError,ValueError):
                return {'available':False,'error':'plane status unavailable'}
        import asyncio
        cp,dp=await asyncio.gather(sample(self.cp),sample(self.dp))
        return {'schema':1,'provider':'pa5200','cp':cp,'dp':dp,
            'forwarding_verified':False,'session_offload_available':False,
            'supported_preparation':list(DP_STAGES),
            'supported_runtime':list(DP_RUNTIME),
            'session_lifecycle':{'implemented':['encode','journal','recover','invalidate'],
                'hardware_adapter_qualified':False},
            'limitations':(['TCAM readback recovery pending'] if
                not cp['available'] or cp['data'].get('recovery_required',True) else []) + [
                'PKI/PKO physical packet qualification pending',
                'FE100 native session adapter requires a qualified owner endpoint']}

    async def prepare(self,operation,expected_boot_id):
        if operation not in DP_STAGES:
            raise ValueError('unsupported preparation operation')
        if not isinstance(expected_boot_id,str) or not expected_boot_id:
            raise ValueError('current DP boot ID required')
        async with self.lock:
            before=await self.dp.status()
            if before.get('boot_id')!=expected_boot_id:
                raise RuntimeError('DP rebooted; refresh hardware status')
            if before.get('ready') is not True:
                raise RuntimeError('DP boot is not ready')
            packet=before.get('packet_initialization',{})
            if packet.get('dma_error',0) or any(packet.get(k)!=0 for k in ('pki_active','pki_enabled','pko_enabled')):
                raise RuntimeError('packet engines unavailable, active or require recovery')
            flag,prerequisites=DP_STAGES[operation]
            if any(packet.get(k) is not True for k in prerequisites):
                raise RuntimeError('initialization prerequisites not satisfied')
            if packet.get(flag) is True:
                return {'changed':False,'operation':operation,'verified':True}
            await self.dp.prepare(operation)
            after=await self.dp.status()
            if after.get('boot_id')!=expected_boot_id or after.get('packet_initialization',{}).get(flag) is not True:
                raise RuntimeError('preparation outcome unverified; refresh before recovery')
            return {'changed':True,'operation':operation,'verified':True}

    async def runtime(self,operation,expected_boot_id):
        if operation not in DP_RUNTIME:
            raise ValueError('unsupported runtime operation')
        if not isinstance(expected_boot_id,str) or not expected_boot_id:
            raise ValueError('current DP boot ID required')
        enabled=DP_RUNTIME[operation]
        async with self.lock:
            before=await self.dp.status()
            if before.get('boot_id')!=expected_boot_id:
                raise RuntimeError('DP rebooted; refresh hardware status')
            packet=before.get('packet_initialization',{})
            trunk=packet.get('trunk',{})
            if packet.get('trunk_registered') is not True or trunk.get('interface')!='ffnpkt0':
                raise RuntimeError('registered FFN trunk required')
            if enabled and (before.get('ready') is not True or
                            packet.get('dma_error') or trunk.get('error')):
                raise RuntimeError('packet runtime unavailable or requires recovery')
            def verified(state):
                p=state.get('packet_initialization',{})
                t=p.get('trunk',{})
                return (state.get('boot_id')==expected_boot_id and
                        t.get('running') is enabled and t.get('error')==0 and
                        t.get('dq_open') is enabled and
                        p.get('pki_enabled')==int(enabled) and
                        p.get('pko_enabled')==int(enabled))
            if verified(before):
                return {'changed':False,'operation':operation,'verified':True}
            await self.dp.prepare(operation)
            if not verified(await self.dp.status()):
                raise RuntimeError('runtime outcome unverified; refresh before recovery')
            return {'changed':True,'operation':operation,'verified':True}


def router(harness,current_user,require_admin,record_audit):
    """Explicit optional WebUI/API mount; importing does not access hardware."""
    from fastapi import APIRouter,Depends,HTTPException
    from pydantic import BaseModel,ConfigDict,Field
    class Preparation(BaseModel):
        model_config=ConfigDict(extra='forbid')
        operation:str=Field(max_length=40)
        boot_id:str=Field(min_length=1,max_length=64)
    api=APIRouter(prefix='/api/pa5200/hardware',tags=['PA-5200 hardware'])
    @api.get('/status')
    async def status(user=Depends(current_user)):
        return await harness.status()
    @api.post('/prepare')
    async def prepare(payload:Preparation,user=Depends(current_user)):
        require_admin(user)
        await record_audit(user['username'],'hardware_prepare',payload.operation)
        try: return await harness.prepare(payload.operation,payload.boot_id)
        except ValueError as error: raise HTTPException(422,str(error))
        except (OSError,RuntimeError):
            raise HTTPException(409,'Hardware preparation unavailable or outcome unverified; refresh status')
    @api.post('/runtime')
    async def runtime(payload:Preparation,user=Depends(current_user)):
        require_admin(user)
        await record_audit(user['username'],'hardware_runtime',payload.operation)
        try: return await harness.runtime(payload.operation,payload.boot_id)
        except ValueError as error: raise HTTPException(422,str(error))
        except (OSError,RuntimeError):
            raise HTTPException(409,'Hardware runtime unavailable or outcome unverified; refresh status')
    return api


if __name__=='__main__':
    import argparse,asyncio,json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('status',*DP_STAGES,*DP_RUNTIME))
    parser.add_argument('--boot-id')
    args=parser.parse_args()
    harness=create_harness('pa5200',installed_adapters)
    result=asyncio.run(harness.status() if args.operation=='status' else
                       (harness.runtime(args.operation,args.boot_id) if args.operation in DP_RUNTIME else
                        harness.prepare(args.operation,args.boot_id)))
    print(json.dumps(result,indent=2))
