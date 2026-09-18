import copy
import collections
import unittest
from unittest.mock import Mock
from ffn_copper_vif import LEASE_SECONDS, CopperVif, validate_profile
from ffn_dp_packet_transport import FRONT, encode, decode_otmh_ssp


def profile(verified=True):
    return {'version':1,'ports':{'2':{'phy':17,'bcm_port':28,'packet_path_verified':verified}}}


def observation():
    return {'port':2,'phy_address':17,'bcm_port':28,'phy_mapping_verified':True,
            'available':True,'enabled':True,'phy_enabled':True,'mac_enabled':True,
            'phy_pending':False,'link':True,'mac_link':True,'datapath_link':True,
            'speed_mbps':1000,'mac_speed_mbps':1000,'packet_path_ready':True}


class Copper(unittest.TestCase):
    def test_copper_ingress_reaches_configured_inspection_engine(self):
        from ffn_inspection import validate, Inspector
        inspector=Inspector.__new__(Inspector)
        inspector.cfg=validate({'revision':0,'mode':'block','ports':[2],'literal':'test'})
        inspector.handle=1;inspector.lib=Mock();inspector.lib.ffn_inline_scan.return_value=2
        inspector.counts=collections.Counter()
        self.assertFalse(inspector.allow(2,bytes(60)))
        self.assertEqual(inspector.counts['port_2_block'],1)
        for port in (0,25,True):
            with self.assertRaises(ValueError):validate(inspector.cfg|{'ports':[port]})

    def setUp(self):
        self.now=100
        self.driver=CopperVif(profile(),clock=lambda:self.now)

    def update(self,row=None):
        self.now+=.01
        self.driver.observe({'token':self.driver.challenge(),'ports':[observation() if row is None else row]})

    def test_corrected_wire_mapping_has_no_port1_alias(self):
        front=self.driver.wire_map(FRONT)
        frame=bytes(range(60))
        self.assertEqual(encode(2,frame,front)[:4],bytes.fromhex('01001c00'))
        self.assertEqual(decode_otmh_ssp(bytes.fromhex('0018001c')+frame,{2,5,13},front),(2,frame))
        self.assertIsNone(decode_otmh_ssp(bytes.fromhex('0018000d')+frame,{2,5,13},front))
        for port in (1,3,4):
            with self.assertRaises(ValueError):encode(port,frame,front)
        self.assertEqual(len(front),len(set(front.values())))

    def test_link_and_commissioning_are_separate(self):
        self.driver=CopperVif(profile(False),clock=lambda:self.now)
        self.update()
        self.assertEqual(self.driver.ports,set())
        self.assertFalse(self.driver.allowed(2))
        self.assertEqual(self.driver.status(2)['speed_mbps'],1000)
        self.assertEqual(self.driver.status(2)['reason'],'packet path not commissioned')

    def test_expiry_duplicate_delayed_and_out_of_order_observations(self):
        token=self.driver.challenge();self.now+=1;new=self.driver.challenge()
        self.driver.observe({'token':new,'ports':[observation()]})
        self.assertTrue(self.driver.allowed(2))
        for old in (token,new):
            with self.assertRaises(ValueError):self.driver.observe({'token':old,'ports':[observation()]})
        self.now+=LEASE_SECONDS
        self.assertFalse(self.driver.allowed(2))
        token=self.driver.challenge();self.now+=LEASE_SECONDS
        with self.assertRaises(ValueError):self.driver.observe({'token':token,'ports':[observation()]})
        self.assertFalse(self.driver.allowed(2))

    def test_slow_inventory_does_not_flap_between_poll_cycles(self):
        for cycle in range(6):
            token=self.driver.challenge()
            self.now+=8  # measured upper end of CP inventory latency
            if cycle:self.assertTrue(self.driver.allowed(2))
            self.driver.observe({'token':token,'ports':[observation()]})
            self.now+=2  # MP timer delay after completing an observation
            self.assertTrue(self.driver.allowed(2))

    def test_disable_pending_mapping_and_speed_mismatch_drop(self):
        for key,value in [('enabled',False),('phy_enabled',False),('mac_enabled',False),('available',False),
                          ('link',False),('mac_link',None),('datapath_link',1),('phy_pending',True),
                          ('phy_mapping_verified',False),('phy_address',16),('bcm_port',13),
                          ('mac_speed_mbps',10000),('speed_mbps',True),('packet_path_ready',False),('packet_path_ready',1)]:
            self.update();self.assertTrue(self.driver.allowed(2))
            row=observation();row[key]=value;self.update(row)
            self.assertFalse(self.driver.allowed(2),key)
        for speed in (100,1000,10000):
            row=observation();row.update(speed_mbps=speed,mac_speed_mbps=speed);self.update(row)
            self.assertTrue(self.driver.allowed(2))

    def test_malformed_and_duplicate_mapping_rejected(self):
        for field,value in [('phy',True),('phy',20),('bcm_port',16),('packet_path_verified',1)]:
            data=profile();data['ports']['2'][field]=value
            with self.assertRaises(ValueError):validate_profile(data)
        data=profile();data['ports']['1']=copy.deepcopy(data['ports']['2'])
        with self.assertRaises(ValueError):validate_profile(data)
        with self.assertRaises(ValueError):self.driver.observe({'token':self.driver.challenge(),'ports':[observation(),observation()]})
        self.driver.observe({'token':self.driver.challenge(),'ports':[]})
        self.assertFalse(self.driver.allowed(2))


if __name__=='__main__':unittest.main()
