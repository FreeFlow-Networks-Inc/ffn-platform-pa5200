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


# Identity page read from the 1000BASE-LX module in faceplate port 5 of the PA-5220 (2026-10-08).
LX=bytes.fromhex('0304070000000200000000010d000a64373700004f454d202020202020202020202020200000005f474c432d4c482d534d4420202020202041202020051e0006001a00004353594745314f433538323620202020323431313031202068f0016f')


def identity(connector=7,codes10g=0,codes1g=0,nominal=13,wavelength=1310,br_max=0,vendor='TEST',part='MODULE'):
    page=bytearray(96);page[0]=3;page[1]=4;page[2]=connector;page[3]=codes10g;page[6]=codes1g;page[12]=nominal
    page[20:36]=vendor.ljust(16).encode();page[40:56]=part.ljust(16).encode();page[60:62]=wavelength.to_bytes(2,'big');page[66]=br_max
    page[63]=sum(page[:63])&255;page[95]=sum(page[64:95])&255
    return bytes(page)


SR_DUAL=identity(codes10g=0x10,codes1g=0x01,nominal=103,wavelength=850,part='AFBR-709SMZ')
COPPER=identity(connector=0x22,codes1g=0x08,nominal=13,wavelength=0,part='SFP-1000T')


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

    def test_module_rates_from_sff8472_codes_and_nominal_rate(self):
        # LX: 1000BASE-LX code, 1.3 GBd nominal, 1310 nm, LC. Dual-rate SR:
        # 10GBASE-SR and 1000BASE-SX codes, 10.3 GBd nominal, 850 nm.
        self.assertEqual(sfp.module_speeds(LX),[1000]);self.assertTrue(sfp.optical(LX))
        self.assertEqual(sfp.module_summary(LX),dict(vendor='OEM',part='GLC-LH-SMD',wavelength_nm=1310,optical=True,speeds=[1000]))
        self.assertEqual(sfp.module_speeds(SR_DUAL),[1000,10000])
        self.assertEqual(sfp.module_speeds(identity(nominal=103,wavelength=850)),[10000])
        self.assertEqual(sfp.module_speeds(identity(nominal=255,br_max=42,wavelength=1310)),[10000])
        self.assertEqual(sfp.module_speeds(identity(nominal=0,wavelength=1310)),[])
        self.assertFalse(sfp.optical(COPPER));self.assertEqual(sfp.module_speeds(COPPER),[1000])
        with self.assertRaises(ValueError):sfp.module_speeds(bytes(96))
        corrupt=bytearray(LX);corrupt[12]=14
        with self.assertRaises(ValueError):sfp.module_speeds(bytes(corrupt))

    def test_link_mode_follows_the_module_and_refuses_undeclared_rates(self):
        self.assertEqual(sfp.link_mode(LX,'auto'),(1000,True));self.assertEqual(sfp.link_mode(LX,'1000'),(1000,True))
        with self.assertRaisesRegex(ValueError,'supports 1000; 10000'):sfp.link_mode(LX,'10000')
        self.assertEqual(sfp.link_mode(SR_DUAL,'auto'),(10000,False));self.assertEqual(sfp.link_mode(SR_DUAL,'1000'),(1000,True))
        self.assertEqual(sfp.link_mode(SR_DUAL,'10000'),(10000,False))
        self.assertIsNone(sfp.link_mode(COPPER,'auto'));self.assertIsNone(sfp.link_mode(identity(nominal=0,wavelength=1310),'auto'))

    def recipe_controller(self,markers):
        path=Path(self.tmp.name)/'recipe.c';path.write_text('prior recipe')
        controller=types.ModuleType('ffn_aggregate_hardware')
        controller.SCRIPT=path;controller.acquire=lambda lock:None
        captured=[]
        def call(request):
            captured.append(path.read_text())
            return dict(ok=True,completed=True,markers=list(markers))
        controller.call=call
        return path,controller,captured

    def test_gigabit_recipe_is_fixed_serialized_and_restores_shared_script(self):
        path,controller,captured=self.recipe_controller(['FFN_SFP_LINK 0 3 1 1000 1 -4'])
        import builtins
        real_open=builtins.open
        def open_lock(path,*args,**kwargs):
            if path=='/run/ffn-forward-test.lock':path=Path(self.tmp.name)/'recipe.lock'
            return real_open(path,*args,**kwargs)
        with patch.dict(sys.modules,ffn_aggregate_hardware=controller),\
             patch.object(sfp,'module_identity',return_value=LX),patch('builtins.open',side_effect=open_lock):
            result=sfp.configure_gigabit_fiber(5,16,'auto')
            self.assertEqual((result['speed'],result['autoneg'],result['link_mode'],result['changed'],result['autoneg_mode_control']),(1000,True,'1000BASE-X',True,-4))
            self.assertIn('BCM_PORT_PHY_CONTROL_AUTONEG_MODE',captured[0])
            self.assertNotIn('0NEG_MODE',captured[0])
            self.assertIn('bcm_port_interface_set(0,16,BCM_PORT_IF_GMII)',captured[0])
            self.assertIn('bcm_port_speed_set(0,16,1000)',captured[0]);self.assertIn('ifn!=3 ||',captured[0])
            self.assertIn('bcm_port_enable_set(0,16,0)',captured[0]);self.assertIn('bcm_port_enable_set(0,16,1)',captured[0])
            self.assertEqual(path.read_text(),'prior recipe')
            # A fixed 1000 on 1 Gb/s optics is the same Clause 37 mode.
            self.assertEqual(sfp.configure_fiber(5,16,'1000')['autoneg'],True)
            with self.assertRaises(ValueError):sfp.configure_gigabit_fiber(5,16,'10000')
            controller.call=lambda r:dict(ok=True,completed=False,markers=[])
            with self.assertRaises(RuntimeError):sfp.configure_gigabit_fiber(5,16,'auto')
            self.assertEqual(path.read_text(),'prior recipe')
            controller.call=lambda r:dict(ok=True,completed=True,markers=['FFN_SFP_LINK 0 4 0 1000 1 0'])
            with self.assertRaisesRegex(RuntimeError,'readback mismatch'):sfp.configure_fiber(5,16,'auto')
        with patch.dict(sys.modules,ffn_aggregate_hardware=controller),\
             patch.object(sfp,'module_identity',side_effect=OSError('no module')):
            self.assertFalse(sfp.configure_fiber(5,16,'auto'))
        with patch.dict(sys.modules,ffn_aggregate_hardware=controller),patch.object(sfp,'module_identity',return_value=COPPER):
            self.assertFalse(sfp.configure_fiber(5,16,'auto'))

    def test_ten_gigabit_optics_restore_the_native_interface_without_autoneg(self):
        path,controller,captured=self.recipe_controller(['FFN_SFP_LINK 0 10 0 10000 1 0'])
        import builtins
        real_open=builtins.open
        def open_lock(path,*args,**kwargs):
            if path=='/run/ffn-forward-test.lock':path=Path(self.tmp.name)/'recipe.lock'
            return real_open(path,*args,**kwargs)
        with patch.dict(sys.modules,ffn_aggregate_hardware=controller),\
             patch.object(sfp,'module_identity',return_value=SR_DUAL),patch('builtins.open',side_effect=open_lock):
            result=sfp.configure_fiber(5,16,'auto')
            self.assertEqual((result['speed'],result['autoneg'],result['link_mode']),(10000,False,'XFI'))
            self.assertIn('bcm_port_interface_set(0,16,BCM_PORT_IF_XFI)',captured[0]);self.assertIn('bcm_port_speed_set(0,16,10000)',captured[0])
            self.assertIn('ifn!=10 ||',captured[0]);self.assertNotIn('bcm_port_autoneg_set(0,16,1)',captured[0].split('if(rv==0 && 0)')[0][-40:])
            controller.call=lambda r:dict(ok=True,completed=True,markers=['FFN_SFP_LINK 0 3 1 1000 1 -4'])
            self.assertEqual(sfp.configure_fiber(5,16,'1000')['link_mode'],'1000BASE-X')

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
