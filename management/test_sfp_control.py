import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import ffn_sfp_control as sfp
import ffn_faceplate as face


class SfpControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registers={0x22:bytearray([0xfe,0xff,0xff,0xff,0,0,0xff,0xff]),
                        0x23:bytearray([0xff,0xff,0xff,0xff,0,0,0xff,0xff])}
        self.writes=[]
        def read(bus,address,offset,count):
            self.assertEqual(bus,1)
            return bytes(self.registers[address][(offset & ~1) | ((offset+i)&1)] for i in range(count))
        def write(bus,address,register,value):
            self.assertEqual((bus,address),(1,0x23))
            self.registers[0x23][register]=value
            self.writes.append((register,value))
            # Model driven pins and pulled-up undriven TX_DISABLE pins.
            for bank in (0,1):
                raw=self.registers[0x23][2+bank] | self.registers[0x23][6+bank]
                self.registers[0x23][bank]=raw ^ self.registers[0x23][4+bank]
        for mock in (patch.object(sfp,'LOCK',Path(self.tmp.name)/'lock'),
                     patch.object(sfp,'read_regs',side_effect=read),
                     patch.object(sfp,'write_reg',side_effect=write)):
            mock.start();self.addCleanup(mock.stop)

    def test_first_sfp_is_bit_zero_and_disable_preserves_other_cages(self):
        before=copy.deepcopy(self.registers)
        self.assertFalse(sfp.inventory()[5]['tx_enabled'])
        sfp.set_enabled(5,True)
        self.assertEqual(self.writes,[(2,0xfe),(6,0xfe)])
        self.assertTrue(sfp.inventory()[5]['present'])
        self.assertFalse(sfp.inventory()[6]['present'])
        self.assertEqual(self.registers[0x22],before[0x22])
        sfp.set_enabled(5,True)
        self.assertEqual(len(self.writes),2,'unchanged enable must not write')
        sfp.set_enabled(5,False)
        self.assertEqual(self.writes[-1],(2,0xff))
        self.assertTrue(sfp.inventory()[5]['tx_disable'])

    def test_all_sfp_bits_and_presence_polarity(self):
        self.assertEqual(sorted(sfp.BITS),list(range(16)))
        for port in range(5,21):
            self.registers[0x23][:]=[0xff,0xff,0xff,0xff,0,0,0xff,0xff]
            sfp.set_enabled(port,True)
            self.assertEqual([p for p,r in sfp.inventory().items() if r['tx_enabled']],[port])
        self.registers[0x22][0]^=0xff;self.registers[0x22][4]=0xff
        self.assertTrue(sfp.inventory()[5]['present'])
        for port in (True,4,21,'5'):
            with self.assertRaises(ValueError):sfp.set_enabled(port,True)

    def test_input_readback_failure_is_not_acknowledged(self):
        with patch.object(sfp,'write_reg'):
            with self.assertRaisesRegex(RuntimeError,'readback'):sfp.set_enabled(5,True)

    def test_diagnostics_calibration_checksums_and_readiness(self):
        identity=bytearray(96);identity[0]=3;identity[92]=0x58;identity[93]=0x90
        identity[20:36]=b'Test Vendor     ';identity[40:56]=b'Test SFP        '
        identity[63]=sum(identity[:63])&255;identity[95]=sum(identity[64:95])&255
        data=bytearray(128);data[56:76]=struct.pack('>5f',0,0,0,1,0)
        for offset in (76,80,84,88):data[offset:offset+4]=struct.pack('>Hh',256,0)
        data[95]=sum(data[:95])&255
        data[96:106]=struct.pack('>hHHHH',41*256,32679,11856,2871,2512)
        decoded=sfp.decode_diagnostics(identity,data)
        self.assertAlmostEqual(decoded['tx_power_dbm'],-5.42,places=2)
        self.assertAlmostEqual(decoded['rx_power_dbm'],-6,places=2)
        self.assertIsNone(decoded['tx_fault'],'unsupported flags must be unknown')
        self.assertEqual(decoded['temperature_c'],41)
        data[110]=1
        with self.assertRaisesRegex(ValueError,'not ready'):sfp.decode_diagnostics(identity,data)
        data[110]=0;identity[40]^=1
        with self.assertRaisesRegex(ValueError,'checksum'):sfp.decode_diagnostics(identity,data)

    def test_faceplate_requires_transmitter_and_mac_ack(self):
        ports=[dict(port=p,enabled=True,link=True,speed_mb=1000) for p in face.PORTS]
        def call(request):
            if request['op']=='port.list':return {'ports':ports}
            if request['op']=='port.set':
                next(p for p in ports if p['port']==request['port'])['enabled']=request['enable']
                return {}
            return {'supported_speeds':[1000,10000],'configured_speed':'1000'}
        with patch.object(face,'STATE',Path(self.tmp.name)/'state.json'),patch.object(face,'call',side_effect=call),\
             patch.object(face,'copper_inventory',side_effect=OSError),\
             patch.object(face,'sfp_inventory',side_effect=sfp.inventory),patch.object(face,'sfp_apply',side_effect=sfp.set_enabled):
            current=face.observe()
            self.assertTrue(current['ports'][4]['mac_link'])
            self.assertFalse(current['ports'][4]['link'],'disabled laser must mask false MAC link')
            result=face.apply(dict(revision=current['revision'],port=5,enabled=True))
            self.assertTrue(result['data']['ports'][4]['enabled'])
            current=face.observe()
            with patch.object(face,'sfp_apply'):
                with self.assertRaisesRegex(RuntimeError,'readback'):
                    face.apply(dict(revision=current['revision'],port=5,enabled=False))
            self.assertIn('pending',json.loads(face.STATE.read_text()))

    def test_gigabit_recipe_is_fixed_serialized_and_restores_shared_script(self):
        path=Path(self.tmp.name)/'recipe.c';path.write_text('prior recipe')
        controller=types.ModuleType('ffn_aggregate_hardware')
        controller.SCRIPT=path;controller.acquire=lambda lock:None
        captured=[]
        def call(request):
            captured.append(path.read_text())
            return dict(ok=True,completed=True,markers=['FFN_SFP_LINK 0 3 1 1 1'])
        controller.call=call
        import builtins
        real_open=builtins.open
        def open_lock(path,*args,**kwargs):
            if path=='/run/ffn-forward-test.lock':path=Path(self.tmp.name)/'recipe.lock'
            return real_open(path,*args,**kwargs)
        with patch.dict(sys.modules,ffn_aggregate_hardware=controller),\
             patch.object(sfp,'gigabit_fiber',return_value=True),patch('builtins.open',side_effect=open_lock):
            self.assertTrue(sfp.configure_gigabit_fiber(5,16,'auto'))
            self.assertIn('BCM_PORT_PHY_CONTROL_AUTONEG_MODE',captured[0])
            self.assertNotIn('0NEG_MODE',captured[0])
            self.assertIn('bcm_port_interface_set(0,16,BCM_PORT_IF_GMII)',captured[0])
            self.assertEqual(path.read_text(),'prior recipe')
            with self.assertRaises(ValueError):sfp.configure_gigabit_fiber(5,16,'10000')
            controller.call=lambda r:dict(ok=True,completed=False,markers=[])
            with self.assertRaises(RuntimeError):sfp.configure_gigabit_fiber(5,16,'auto')
            self.assertEqual(path.read_text(),'prior recipe')

    def test_mux_resolution_follows_channels_not_vendor_adapter_numbers(self):
        root=Path(self.tmp.name);mux=root/'1-0075';mux.mkdir()
        adapter=root/'i2c-42';adapter.mkdir()
        driver=root/'pca954x';driver.mkdir()
        (mux/'driver').symlink_to(driver,target_is_directory=True)
        (mux/'channel-0').symlink_to(adapter,target_is_directory=True)
        with patch.object(sfp,'SYSFS',root):
            self.assertEqual(sfp.module_bus(5),42)
            with self.assertRaises(OSError):sfp.module_bus(6)


if __name__=='__main__':unittest.main()
