#!/usr/bin/env python3
"""Recovery adapter for supervisor-owned isolated physical FE100 tests.

This is commissioning support, not a production admission provider. The parent
is the sole cleanup owner. Its child records allocation intent before SDK
calls and never deletes BCM resources. An interrupted allocation or deletion
is uncertain and blocks reuse; hardware IDs are never guessed or adopted.
"""
import json
from pathlib import Path
from ffn_fe100_guard import publish


class Recovery:
    def __init__(self, path, *, epoch, lab_factory, route):
        self.path=Path(path)
        self.epoch,self.lab_factory,self.route=epoch,lab_factory,route

    def __call__(self, reason):
        if not self.path.exists():
            if reason!='startup recovery':raise RuntimeError('missing physical recovery journal')
            return True
        record=json.loads(self.path.read_text())
        if (record.get('schema')!=1 or record.get('cleanup_owner')!='guardian' or
                record.get('bcm_epoch')!=self.epoch()):
            raise RuntimeError('physical recovery ownership or BCM lifetime changed')
        if record.get('stage')=='restored' and record.get('withdrawal_acknowledged') is True:
            return True
        # Reopening the lab validates the CP boot, profile, native owner and
        # initialization generation, under its exclusive FE100 table lock.
        lab=self.lab_factory(recovery=record['lab_journal'])
        def save():publish(self.path,record)
        def route(mode,ids=None):
            result=self.route(mode,ids)
            if not isinstance(result,dict) or result.get('completed') is not True:
                raise RuntimeError('BCM withdrawal not acknowledged: '+mode)
            return result
        try:
            if record.get('redirect_touched'):
                modes=record.get('redirects',['front5-session-restore'])
                if (not isinstance(modes,list) or not 1<=len(modes)<=2 or len(set(modes))!=len(modes) or
                        any(mode not in ('front5-session-restore','session-path-restore') for mode in modes)):
                    raise ValueError('invalid ingress withdrawal intents')
                for mode in reversed(modes):
                    result=route(mode)
                    record.setdefault('ingress_withdrawal',{})[mode]=result;save()
            # Native lookups verify absence of both directions before any
            # next-hop, QMAP or LIF resource is restored.
            lab.restore()
            record['fe100']=lab.record;save()
            if record.get('rule_pending'):
                raise RuntimeError('uncertain BCM allocation; automatic ID adoption refused')
            if record.get('delete_pending') is not None:
                raise RuntimeError('uncertain BCM deletion; automatic ID reuse refused')
            for index, ids in reversed(list(enumerate(record.get('rules',[])))):
                if index in record.get('rules_removed',[]):continue
                record['delete_pending']=index;save()
                route('offload-rule-delete',ids)
                route('session-group-absent',ids)
                record.setdefault('rules_removed',[]).append(index)
                record['delete_pending']=None;save()
            # Punt-path experiments require their own ownership/readback
            # recovery adapter; never claim full cleanup without it.
            if record.get('punt_before'):raise RuntimeError('unsupported punt recovery profile')
            record.update(stage='restored',cleanup_errors=[],withdrawal_acknowledged=True)
            save()
            return True
        except BaseException as error:
            record.update(stage='recovery-required',cleanup_errors=[str(error)],withdrawal_acknowledged=False)
            save();raise
        finally:lab.close()
