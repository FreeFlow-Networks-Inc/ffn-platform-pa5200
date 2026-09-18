import copy
import json
import struct
import unittest
from unittest.mock import patch
import ffn_aggregate_vlan as vlan


class VlanTests(unittest.TestCase):
    def setUp(self):
        self.unit=dict(name='ae1.69',tag=69,mtu=1500,addresses=['192.0.2.1/24'],management=dict(profile='ping',ping=True,tcp=[],udp=[],sources=[]))
        self.network=dict(enabled=False,addresses=[],mtu=1500,units=[self.unit])
        self.plain=b'\xff'*6+b'\x02\x00\x00\x00\x00\x01'+b'\x08\x00'+b'\x45'+b'\0'*15+b'\xc0\x00\x02\x01'
    def tagged(self,tag,frame=None):
        plain=frame or self.plain
        return plain[:12]+b'\x81\x00'+struct.pack('!H',tag)+plain[12:]
    def test_link_only_parent_carries_only_configured_vlan(self):
        self.assertTrue(vlan.carrying(self.network))
        self.assertEqual(vlan.classify('ae1',self.network,self.tagged(69)),('ae1.69',self.plain,True))
        self.assertIsNone(vlan.classify('ae1',self.network,self.plain))
        for frame in (self.tagged(70),self.tagged(0),self.tagged(4095),self.tagged(69,self.tagged(69)),self.tagged(69)[:16]):
            self.assertIsNone(vlan.classify('ae1',self.network,frame))
    def test_vlan_priority_and_mtu_and_scoped_local_address(self):
        self.assertEqual(vlan.classify('ae1',self.network,self.tagged(69|0xa000))[0],'ae1.69')
        self.assertIsNone(vlan.classify('ae1',self.network,self.tagged(69)+b'\0'*1500))
        other=dict(self.unit,name='ae1.70',tag=70,addresses=['198.51.100.1/24'])
        self.network['units'].append(other)
        self.assertFalse(vlan.classify('ae1',self.network,self.tagged(70))[2])
    def test_invalid_settings(self):
        for change in (dict(name='ae2.69'),dict(tag=0),dict(tag=True),dict(mtu=9000),dict(addresses=['bad']),dict(addresses=['2001:db8::1/64'],mtu=1000)):
            with self.assertRaises(ValueError):vlan.validate('ae1',dict(self.network,units=[dict(self.unit,**change)]))
        with self.assertRaises(ValueError):vlan.validate('ae1',dict(self.network,units=[self.unit,self.unit]))
    def test_foreign_child_is_rejected_before_mutation(self):
        events=[]
        def ip(*args):
            events.append(args)
            return json.dumps([dict(ifname='ae1',ifindex=7,ifalias='ffn-aggregate:token'),dict(ifname='ae1.69',ifalias='someone-else')])
        with self.assertRaisesRegex(ValueError,'foreign'):
            vlan.reconcile('fixture','ae1','token',self.network,ip,lambda *a:None)
        self.assertEqual(events,[('-d','-j','link')])

if __name__=='__main__':unittest.main()
