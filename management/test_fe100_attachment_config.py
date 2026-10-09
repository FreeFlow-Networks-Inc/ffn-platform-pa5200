import hashlib
import unittest
import tempfile
from pathlib import Path
from fe100_attachment_config import compile_config,validate

XML=b'''<config><devices><entry name="local"><network><interface>
<ethernet><entry name="ethernet1/3"><layer3/></entry>
<entry name="ethernet1/9"><aggregate-group>ae4</aggregate-group></entry>
<entry name="ethernet1/17"><aggregate-group>ae4</aggregate-group></entry>
<entry name="ethernet1/24"/></ethernet>
<aggregate-ethernet><entry name="ae4"><aggregate-only>yes</aggregate-only><layer3><units>
<entry name="ae4.82"><tag>82</tag></entry></units></layer3></entry></aggregate-ethernet>
</interface></network><vsys><entry name="vsys2"><import><network><interface>
<member>ethernet1/3</member><member>ae4</member></interface></network></import><zone>
<entry name="inside"><network><layer3><member>ae4.82</member></layer3></network></entry>
<entry name="outside"><network><layer3><member>ethernet1/3</member></layer3></network></entry>
</zone></entry></vsys></entry></devices><admin><password>DO-NOT-TRANSMIT</password></admin></config>'''


class Configuration(unittest.TestCase):
    def test_dynamic_ports_vlan_zones_and_only_l3_intent(self):
        value=compile_config(XML);rows={r['name']:r for r in value['interfaces']}
        self.assertEqual(set(rows),{'ethernet1/3','ae4.82'})
        self.assertEqual(rows['ae4.82'],dict(name='ae4.82',parent='ae4',kind='aggregate',ports=[9,17],
            vlan=82,zone='inside',vsys='vsys2',enabled=True))
        self.assertEqual(rows['ethernet1/3']['vlan'],0)
        self.assertEqual(value['config_digest'],hashlib.sha256(XML).hexdigest())
        self.assertNotIn('DO-NOT-TRANSMIT',str(value))
    def test_disabled_members_do_not_gain_enabled_intent(self):
        value=compile_config(XML.replace(b'<aggregate-group>ae4',b'<link-state>down</link-state><aggregate-group>ae4',1))
        self.assertFalse(value['interfaces'][0]['enabled'])
    def test_missing_zone_is_explicit(self):
        value=compile_config(XML.replace(b'<member>ethernet1/3</member></layer3>',b'</layer3>'))
        self.assertIsNone(value['interfaces'][1]['zone'])
    def test_duplicate_or_cross_vsys_zone_ownership_rejected(self):
        duplicate=XML.replace(b'</zone>',b'<entry name="duplicate"><network><layer3><member>ae4.82</member></layer3></network></entry></zone>')
        with self.assertRaises(ValueError):compile_config(duplicate)
    def test_defaults_are_not_automatically_enabled(self):
        self.assertEqual(compile_config(b'<config><devices><entry><network><interface><ethernet><entry name="ethernet1/1"/></ethernet></interface></network></entry></devices></config>')['interfaces'],[])
    def test_invalid_vlan_and_unsafe_xml_rejected(self):
        for raw in (XML.replace(b'<tag>82',b'<tag>4095'),b'<!DOCTYPE config []>'+XML,XML.decode().encode('utf-16')):
            with self.assertRaises((ValueError,UnicodeError)):compile_config(raw)
    def test_normalized_input_cannot_inject_table_indices_or_boolean_ports(self):
        value=compile_config(XML);value['interfaces'][0]['ports']=[True]
        with self.assertRaises(ValueError):validate(value)
        value=compile_config(XML);value['interfaces'][0]['miss_next_hop']=1
        with self.assertRaises(ValueError):validate(value)
    def test_committed_file_change_requires_relay_resynchronization(self):
        from session_relay import verify_configuration
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'config.xml';p.write_bytes(XML)
            verify_configuration(p,hashlib.sha256(XML).hexdigest())
            p.write_bytes(XML.replace(b'82',b'83'))
            with self.assertRaisesRegex(RuntimeError,'resynchronization'):
                verify_configuration(p,hashlib.sha256(XML).hexdigest())


if __name__=='__main__':unittest.main()
