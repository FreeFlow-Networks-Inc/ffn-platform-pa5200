from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_physical_forwarding as hw


class PhysicalHardwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.enabled=True;self.redirect=False;self.events=[]
        def hardware(port,mode):
            self.assertEqual(port,5)
            if mode:self.redirect=mode==1;self.events.append(('redirect',self.redirect))
            return dict(header=11,wan_queues=8,trunk_queues=8,destination=24,enabled=int(self.redirect))
        def call(payload):
            if payload['op']=='port.set':self.enabled=payload['enable'];self.events.append(('enabled',self.enabled))
            return dict(ports=[dict(port=16,enabled=self.enabled,link=False)])
        def ensure(ports,epoch):
            self.assertEqual(ports,[5]);self.assertFalse(self.enabled);self.events.append(('queues',True))
        values={'Path':lambda p:self.root/Path(p).name,'FACEPLATE_LOCK':self.root/'face','AGGREGATES':self.root/'aggregates',
                'epoch':lambda:'epoch','hardware':hardware,'call':call,'ensure':ensure}
        for name,value in values.items():
            p=patch.object(hw,name,value);p.start();self.addCleanup(p.stop)
        self.request=dict(port=5,epoch='epoch',dp_boot_id='985984f6-7566-4152-9d31-8b5ce14ba5db')
    def test_disconnected_port_redirect_precedes_enable(self):
        self.assertTrue(hw.execute('start',self.request)['ready'])
        self.assertEqual(self.events,[('enabled',False),('queues',True),('redirect',True),('enabled',True)])
        self.events=[];self.assertFalse(hw.execute('stop',self.request)['ready'])
        self.assertEqual(self.events,[('enabled',False),('redirect',False)])
    def test_unowned_redirect_and_changed_epoch_are_not_adopted(self):
        self.redirect=True
        with self.assertRaisesRegex(ValueError,'Unowned'):hw.execute('start',self.request)
        self.redirect=False
        with self.assertRaisesRegex(ValueError,'lifetime'):hw.execute('start',dict(self.request,epoch='old'))
        self.assertEqual(self.events,[])
    def test_ambiguous_queue_preparation_stays_disabled(self):
        with patch.object(hw,'ensure',side_effect=RuntimeError('uncertain')):
            with self.assertRaisesRegex(RuntimeError,'uncertain'):hw.execute('start',self.request)
        self.assertFalse(self.enabled);self.assertFalse(self.redirect)

    def test_failed_withdrawal_does_not_allocate_or_redirect(self):
        with patch.object(hw,'call',return_value={'ports':[{'port':16,'enabled':True}]}):
            with self.assertRaisesRegex(RuntimeError,'withdrawal'):hw.execute('start',self.request)
        self.assertEqual(self.events,[])


if __name__=='__main__':unittest.main()
