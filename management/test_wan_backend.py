import unittest
from wan_backend import execute
BOOT='985984f6-7566-4152-9d31-8b5ce14ba5db'
class Backend(unittest.TestCase):
    def setUp(self):
        self.calls=[];self.failure=None;self.available=True
        self.payload={'revision':3,'operation':'probe','expected_boot_id':BOOT}
    def call(self,role,op,payload):
        self.calls.append((role,op))
        if op==self.failure:raise RuntimeError('lost response')
        if (role,op)==('cp','status'):return {'revision':3,'state':{'pending':None,'enabled':False}}
        if (role,op)==('dp','status'):return {'boot_id':BOOT,'fabric_available':self.available}
        return {}
    def drain(self,data):self.calls.append(('mp','drain'));return {'drained':True}
    def test_order_and_cleanup(self):
        result=execute('apply',self.payload,self.call,self.drain)
        self.assertFalse(result['internet_ready'])
        self.assertEqual(self.calls,[('cp','status'),('dp','status'),('mp','drain'),('cp','prepare'),('dp','probe'),('cp','finish'),('cp','abort')])
    def test_ambiguous_prepare_and_probe_failure_still_abort(self):
        for failure in ('prepare','probe','finish'):
            self.calls=[];self.failure=failure
            with self.assertRaises(RuntimeError):execute('apply',self.payload,self.call,self.drain)
            self.assertEqual(self.calls[-1],('cp','abort'))
    def test_busy_owner_and_changed_boot_rejected_before_drain(self):
        self.available=False
        with self.assertRaises(ValueError):execute('apply',self.payload,self.call,self.drain)
        self.available=True
        with self.assertRaises(ValueError):execute('apply',self.payload|{'expected_boot_id':'cc3107f8-eb91-4c58-ae04-b25f8fa4a988'},self.call,self.drain)
        self.assertNotIn(('mp','drain'),self.calls)
    def test_stale_revision_and_validation_never_mutate(self):
        with self.assertRaises(ValueError):execute('apply',self.payload|{'revision':2},self.call,self.drain)
        self.assertEqual(self.calls,[('cp','status')]);self.calls=[]
        self.assertTrue(execute('validate',self.payload,self.call,self.drain)['validated'])
        self.assertEqual(self.calls,[('cp','status'),('dp','status')])

    def test_attach_recovers_without_external_traffic_probe(self):
        def call(role,op,payload):
            self.calls.append((role,op))
            if (role,op)==('cp','status'):
                return {'revision':3,'epoch':'new','state':{'epoch':'old','enabled':True},'wire_qualified':False}
            if (role,op)==('dp','status'):return {'boot_id':BOOT,'fabric_available':True}
            if op=='attachment-status':return {'running':False}
            if op=='recover':return {'revision':4}
            if op=='finish':return {'revision':6,'wire_qualified':True}
            if (role,op)==('cp','start'):
                self.assertEqual(payload,{'revision':4,'dp_boot_id':BOOT})
                return {'revision':7,'ready':{'1':True}}
            if (role,op)==('dp','start'):return {'running':True}
            return {}
        result=execute('apply',self.payload|{'operation':'attach'},call,self.drain)
        self.assertTrue(result['attachment']['running'])
        self.assertEqual(self.calls,[('cp','status'),('dp','status'),('dp','attachment-status'),
            ('mp','drain'),('cp','recover'),('cp','start'),('dp','start')])
    def test_attach_recovers_missing_fabric_after_boot_without_wire_probe(self):
        ready=False
        def call(role,op,payload):
            nonlocal ready
            self.calls.append((role,op))
            if (role,op)==('cp','status'):
                return {'revision':3,'epoch':'same','state':{'epoch':'same','enabled':False},'wire_qualified':False}
            if (role,op)==('dp','status'):
                return {'boot_id':BOOT,'boot_ready':True,'fabric_available':True,'fabric_ready':ready}
            if op=='attachment-status':return {'running':False}
            if op=='reconcile':
                self.assertEqual(payload,{'boot_id':BOOT});ready=True
                return {'ready':True,'boot_id':BOOT}
            if op=='finish':return {'revision':4,'wire_qualified':True}
            if (role,op)==('cp','start'):return {'revision':5,'ready':{'1':True}}
            if (role,op)==('dp','start'):return {'running':True}
            return {}
        request=self.payload|{'operation':'attach'}
        self.assertTrue(execute('validate',request,call,self.drain)['validated'])
        self.assertNotIn(('dp','reconcile'),self.calls);self.calls=[]
        self.assertTrue(execute('apply',request,call,self.drain)['attachment']['running'])
        self.assertLess(self.calls.index(('mp','drain')),self.calls.index(('dp','reconcile')))
        self.assertLess(self.calls.index(('dp','reconcile')),self.calls.index(('cp','start')))
        ready=False;self.calls=[]
        def stale(role,op,payload):
            value=call(role,op,payload)
            if op=='reconcile':value['boot_id']='changed'
            return value
        with self.assertRaisesRegex(RuntimeError,'not acknowledged'):
            execute('apply',request,stale,self.drain)
        self.assertNotIn(('cp','prepare'),self.calls)

if __name__=='__main__':unittest.main()
