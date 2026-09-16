import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_copper_forwarding as owner


def hardware(up=True):
    return {'header':11,'trunk_queues':8,'ports':{
        p:{'destination':24 if up else 0,'enabled':up,'queues':8} for p in (14,15)}}


class CopperForwarding(unittest.TestCase):
    def test_readiness_requires_current_owner_intent_and_every_queue(self):
        saved={'epoch':'now','enabled':True,'pending':None}
        proof={'schema':1,'epoch':'now','ports':[3,4]}
        self.assertEqual(owner.readiness(saved,proof,'now',hardware()),{'3':True,'4':True})
        for state,evidence,epoch,hw in (
            (saved,proof,'new',hardware()),(saved,{},'now',hardware()),
            (saved|{'pending':'start'},proof,'now',hardware()),
            (saved|{'enabled':False},proof,'now',hardware()),
            (saved,proof,'now',hardware()|{'header':2}),
            (saved,proof,'now',hardware()|{'trunk_queues':0})):
            self.assertFalse(any(owner.readiness(state,evidence,epoch,hw).values()))
        hw=hardware();hw['ports'][15]['queues']=16
        self.assertFalse(owner.readiness(saved,proof,'now',hw)['4'])

    def test_failures_stay_pending_until_verified_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);state=base/'state';proof=base/'proof'
            proof.write_text(json.dumps({'schema':1,'epoch':'now','ports':[3,4]}))
            with patch.object(owner,'STATE',state),patch.object(owner,'QUALIFIED',proof),\
                 patch.object(owner,'LOCK',base/'lock'),patch.object(owner,'epoch',return_value='now'),\
                 patch.object(owner,'run',side_effect=[hardware(False),RuntimeError('timeout')]):
                with self.assertRaises(RuntimeError):owner.execute('start')
                self.assertEqual(json.loads(state.read_text())['pending'],'start')
            with patch.object(owner,'STATE',state),patch.object(owner,'QUALIFIED',proof),\
                 patch.object(owner,'LOCK',base/'lock'),patch.object(owner,'epoch',return_value='now'),\
                 patch.object(owner,'run',side_effect=[hardware(),hardware(False)]):
                result=owner.execute('recover')
                self.assertFalse(any(result['ready'].values()))
                self.assertIsNone(result['state']['pending'])

    def test_stale_cleanup_and_foreign_redirects_are_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);state=base/'state';proof=base/'proof'
            state.write_text(json.dumps({'epoch':'old','enabled':False}))
            proof.write_text(json.dumps({'schema':1,'epoch':'now','ports':[3,4]}))
            with patch.object(owner,'STATE',state),patch.object(owner,'QUALIFIED',proof),\
                 patch.object(owner,'LOCK',base/'lock'),patch.object(owner,'epoch',return_value='now'),\
                 patch.object(owner,'run',return_value=hardware()) as run:
                for op in ('start','stop','recover'):
                    with self.assertRaises(RuntimeError):owner.execute(op)
                self.assertEqual(run.call_args_list,[((0,),)]*3)

    def test_incomplete_or_ambiguous_readbacks_fail(self):
        response={'ok':True,'completed':True,'markers':[
            'FFN_CF_HEADER value=11 queues=8 rv=0',
            'FFN_CF_PORT port=14 dst=24 enabled=1 queues=8 rv=0',
            'FFN_CF_PORT port=15 dst=24 enabled=1 queues=8 rv=0','FFN_CF_DONE']}
        self.assertEqual(owner.parse(response),hardware())
        for r in (response|{'completed':False},response|{'truncated':True},
                  response|{'markers':response['markers'][:-2]+['FFN_CF_DONE']},
                  response|{'markers':response['markers'][:2]+response['markers'][1:]}):
            with self.assertRaises(RuntimeError):owner.parse(r)


if __name__=='__main__':unittest.main()
