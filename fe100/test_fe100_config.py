from dataclasses import FrozenInstanceError
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import ffn_fe100_config as config


class Fe100Config(unittest.TestCase):
    def setUp(self):
        self.xml=Path(__file__).with_name('fe100.cfgdb.xml').read_bytes()

    def test_shipped_profile_and_native_offsets_preserve_other_data(self):
        profile=config.parse_profile(self.xml)
        template=bytes(i%256 for i in range(2812))
        native=config.native_configuration(template,profile)
        self.assertEqual(struct.unpack_from('>I',native,0),(1,))
        self.assertEqual(struct.unpack_from('>II',native,1400),(4,2))
        for i in range(2812):
            if i not in (*range(4),*range(1400,1408)):self.assertEqual(native[i],template[i])
        self.assertEqual(template,bytes(i%256 for i in range(2812)))
        with self.assertRaises(FrozenInstanceError):profile.usecase=2

    def test_rejects_unsupported_and_misleading_input(self):
        bad=[b'',b'x'*4097,self.xml.replace(b'"usecase": 1',b'"usecase": true'),
             self.xml.replace(b'"usecase": 1',b'"usecase": 2'),
             self.xml.replace(b'"cfg_mode": 4',b'"cfg_mode": 3'),
             self.xml.replace(b'"v4_v6_choice": 2',b'"v4_v6_choice": 0'),
             self.xml.replace(b'"usecase": 1',b'"usecase": 1, "usecase": 1'),
             self.xml.replace(b'"usecase": 1',b'"usecase": 1, "offload": true'),
             self.xml.replace(b'hw.fe100',b'hw.other'),
             self.xml.replace(b'<value>',b'<value other="1">'),
             self.xml.replace(b'<configdb>',b'<configdb>unexpected'),
             b'<!DOCTYPE configdb [<!ENTITY x "1">]>'+self.xml,
             b'<configdb><entry name="hw.fe100"><value>+ __import__("os")</value></entry></configdb>',
             self.xml.replace(b'</configdb>',b'<entry name="hw.fe100"/></configdb>')]
        for value in bad:
            with self.subTest(value=value[:80]),self.assertRaises(ValueError):config.parse_profile(value)

    def test_missing_default_only_and_no_fallback_on_bad_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fe100.cfgdb.xml'
            with patch.object(config,'CONFIG',path):
                self.assertEqual(config.load_profile(),config.Profile())
                with self.assertRaises(FileNotFoundError):config.load_profile(path)
                path.write_bytes(b'broken')
                with self.assertRaises(ValueError):config.load_profile()
                path.write_bytes(self.xml)
                self.assertEqual(config.load_profile(),config.Profile())

    def test_native_abi_and_direct_profile_validation(self):
        with self.assertRaises(ValueError):config.native_configuration(bytes(2811),config.Profile())
        with self.assertRaises(ValueError):config.native_configuration(bytes(2812),{})
        with self.assertRaises(ValueError):config.Profile(usecase=True)


if __name__=='__main__':unittest.main()
