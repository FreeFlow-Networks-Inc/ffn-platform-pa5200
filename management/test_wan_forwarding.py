from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ffn_wan_forwarding as wan
TOKEN='cc3107f8-eb91-4c58-ae04-b25f8fa4a988'
BOOT='985984f6-7566-4152-9d31-8b5ce14ba5db'
class WAN(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        for name in ('STATE','PROOF','LOCK'):
            item=patch.object(wan,name,Path(folder.name)/name);item.start();self.addCleanup(item.stop)
        self.enabled=0;self.writes=[]
        def hardware(mode):
            if mode:self.enabled=int(mode==1);self.writes.append(mode)
            return {'header':11,'wan_queues':8,'trunk_queues':8,'destination':24,'enabled':self.enabled}
        for name,value in [('epoch',lambda:'current'),('hardware',hardware),('watchdog',lambda token:None)]:
            item=patch.object(wan,name,value);item.start();self.addCleanup(item.stop)
    def prepare(self):
        return wan.execute('prepare',{'revision':0,'token':TOKEN,'dp_boot_id':BOOT})
    def report(self,**values):
        return {'port':1,'bcm_port':28,'boot_id':BOOT,'lease_acquired':False,
                'dhcp_offer_verified':True,'discover_sent':1}|values
    def test_sdk_result_requires_completion_and_exact_readback(self):
        value={'ok':True,'completed':True,'truncated':False,'markers':[
            'FFN_WAN_STATE header=11 wanq=8 trunkq=8 dst=24 enabled=1 rv=0','FFN_WAN_DONE']}
        self.assertEqual(wan.parse(value)['destination'],24)
        for changed in ({'completed':False},{'truncated':True},{'markers':[]},
                        {'markers':value['markers']+value['markers']}):
            with self.assertRaises(RuntimeError):wan.parse(value|changed)
    def test_probe_never_claims_forwarding_ready_and_finishes_disabled(self):
        self.assertFalse(self.prepare()['ready']['1'])
        result=wan.execute('finish',{'token':TOKEN,'report':self.report()})
        self.assertTrue(result['qualified']);self.assertFalse(result['ready']['1'])
        self.assertIsNone(result['state']['pending']);self.assertEqual(self.writes,[1,2])
        wan.execute('expire',{'token':TOKEN});self.assertEqual(self.writes,[1,2])
    def test_negative_probe_revokes_previous_proof(self):
        self.prepare()
        result=wan.execute('finish',{'token':TOKEN,'report':self.report(dhcp_offer_verified=False)})
        self.assertFalse(result['qualified']);self.assertFalse(self.enabled)
    def test_static_wan_attachment_uses_wire_proof_not_dhcp_offer(self):
        self.prepare()
        result=wan.execute('finish',{'token':TOKEN,'report':self.report(dhcp_offer_verified=False,
            counters={'wan_rx':3},return_sources={'28':3})})
        self.assertFalse(result['qualified'])
        with self.assertRaises(RuntimeError):
            wan.execute('start',{'revision':result['revision'],'dp_boot_id':TOKEN})
        active=wan.execute('start',{'revision':result['revision'],'dp_boot_id':BOOT})
        self.assertTrue(active['ready']['1'])
        stopped=wan.execute('stop',{'revision':active['revision']})
        self.assertFalse(stopped['ready']['1'])
    def test_watchdog_disables_interrupted_probe(self):
        self.prepare();result=wan.execute('expire',{'token':TOKEN})
        self.assertFalse(result['state']['enabled']);self.assertIsNone(result['state']['pending'])
    def test_wrong_token_and_new_bcm_owner_never_cleanup(self):
        self.prepare();wan.execute('abort',{'token':BOOT})
        with patch.object(wan,'epoch',lambda:'replacement'):wan.execute('expire',{'token':TOKEN})
        self.assertEqual(self.writes,[1])
    def test_stale_revision_and_timer_failure_never_enable_hardware(self):
        with self.assertRaises(ValueError):wan.execute('prepare',{'revision':8,'token':TOKEN,'dp_boot_id':BOOT})
        with patch.object(wan,'watchdog',side_effect=RuntimeError('timer failed')):
            with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.writes,[])
    def test_wrong_dp_boot_or_expired_probe_cannot_qualify(self):
        self.prepare()
        with self.assertRaises(ValueError):wan.execute('finish',{'token':TOKEN,'report':self.report(boot_id=TOKEN)})
        with patch.object(wan.time,'monotonic',return_value=float('inf')):
            with self.assertRaises(RuntimeError):wan.execute('finish',{'token':TOKEN,'report':self.report()})
        self.assertFalse(wan.PROOF.exists())
        wan.execute('abort',{'token':TOKEN});self.assertFalse(self.enabled)
if __name__=='__main__':unittest.main()
