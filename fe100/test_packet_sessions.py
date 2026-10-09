import struct
import unittest
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch
from ffn_fe100_sessions import key4, forwarding_entry4, validate_entry4
from ffn_fe100_session_adapter import encode_native, decode_native


class PacketSessionTests(unittest.TestCase):
    def test_resource_tables_use_native_owner_without_legacy_workers(self):
        from unittest.mock import Mock
        import ffn_fe100_packet_lab as lab
        owner=object.__new__(lab.Lab);owner.resources=None;owner.lock=Mock()
        with patch('ffn_fe100_resource_tables.ResourceTables') as backend, patch.object(lab.subprocess,'Popen') as legacy:
            io=backend.return_value;io.call.return_value=None
            self.assertEqual(owner.call('qm',index=30),dict(rc=3,data='00'*84))
            self.assertEqual(backend.call_args.args[0]['qmap4'],[30,31])
            io.call.assert_called_with('qmap4','fetch',30,None)
            raw=bytes(84);io.call.return_value=raw
            self.assertEqual(owner.call('qm','insert',31,raw.hex())['rc'],0)
            io.call.assert_called_with('qmap4','insert',31,raw)
            # Absent-before snapshots are still passed by the durable journal
            # when removing an entry. Never send those bytes to native delete.
            owner.call('qm','delete',31,raw.hex())
            io.call.assert_called_with('qmap4','delete',31,None)
            for table,size in (('smac',8),('nexthop',16),('lif',36),('lef',10)):
                io.call.return_value=bytes(size)
                self.assertEqual(owner.call(table)['data'],'00'*size)
            backend.assert_called_once();legacy.assert_not_called()

    def test_resource_failure_is_not_acknowledged_or_retried_as_legacy_io(self):
        from unittest.mock import Mock
        import ffn_fe100_packet_lab as lab
        owner=object.__new__(lab.Lab);owner.resources=Mock()
        owner.resources.call.side_effect=TimeoutError('native write uncertain')
        with patch.object(lab.subprocess,'Popen') as legacy:
            with self.assertRaises(TimeoutError):owner.call('qm','insert',30,bytes(84))
            legacy.assert_not_called()

    def test_split_path_has_distinct_zones_and_reverse_next_hop(self):
        import ffn_fe100_packet_lab as source
        from ffn_fe100_sessions import output_key4
        settings=dict(FFN_FE100_LAB_PAIR='11,23',FFN_FE100_FRONT_RETURN='11',
                      FFN_FE100_CROSS='1',FFN_FE100_NAT_LAB='port',FFN_FE100_ROUTED_LAB='1',
                      FFN_FE100_PAIRED_NAT_LAB='1',FFN_FE100_SPLIT_PATH_LAB='1')
        with patch.dict(os.environ,settings):
            spec=importlib.util.spec_from_file_location('split_lab_fixture',source.__file__)
            lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        self.assertEqual(lab.KEY[2:4],(4094).to_bytes(2,'big'))
        self.assertEqual(lab.REVERSE_IDENTITY[2:4],(4093).to_bytes(2,'big'))
        self.assertEqual(int.from_bytes(lab.FORWARD[20:24],'big'),31)
        self.assertEqual(int.from_bytes(lab.REVERSE_FORWARD[20:24],'big'),30)
        def reverse(key):return key[6:8]+key[4:6]+key[12:16]+key[8:12]
        self.assertEqual(lab.REVERSE_IDENTITY[4:16],reverse(output_key4(lab.FORWARD)))
        self.assertEqual(output_key4(lab.REVERSE_FORWARD)[4:16],reverse(lab.KEY))

    def test_recovery_refuses_changed_owner_profile_boot_and_generation(self):
        import copy
        import ffn_fe100_packet_lab as lab
        record=dict(schema=2,owner_sha256=lab.SHA,cp_boot_id='boot',profile=lab.recovery_profile(),
                    generation_sources={'flu-init-boot.json':'a'*64},changes=[])
        lab.Lab.validate_recovery(record,'boot',record['generation_sources'])
        for field,value in [('schema',1),('owner_sha256','other'),('cp_boot_id','other'),
                            ('profile',{}),('generation_sources',{})]:
            altered=copy.deepcopy(record);altered[field]=value
            with self.assertRaises(RuntimeError):lab.Lab.validate_recovery(altered,'boot',record['generation_sources'])
        with self.assertRaises(RuntimeError):lab.Lab.validate_recovery(record,'boot',{})

    def test_failed_flow_drain_preserves_dependent_resources(self):
        import ffn_fe100_packet_lab as lab
        owner=object.__new__(lab.Lab);owner.prepared=True
        owner.record=dict(session_touched=True,changes=[dict(kind='nexthop',index=31,restored=False)])
        owner.save=lambda:None
        def conflict(*a,**k):raise RuntimeError('flow ownership conflict')
        owner.remove_session=conflict
        owner.call=lambda *a,**k:self.fail('dependent resource touched before flow drain')
        with self.assertRaisesRegex(RuntimeError,'ownership conflict'):owner.restore()
        self.assertEqual(owner.record['stage'],'recovery_required')

    def test_paired_nat_lab_restores_both_directions_after_partial_install(self):
        import ffn_fe100_packet_lab as source
        settings=dict(FFN_FE100_LAB_PAIR='23,24',FFN_FE100_FRONT_RETURN='23',
                      FFN_FE100_NAT_LAB='port',FFN_FE100_ROUTED_LAB='1',FFN_FE100_PAIRED_NAT_LAB='1')
        with patch.dict(os.environ,settings):
            spec=importlib.util.spec_from_file_location('paired_lab_fixture',source.__file__)
            lab=importlib.util.module_from_spec(spec);spec.loader.exec_module(lab)
        from ffn_fe100_sessions import output_key4
        def reverse(key):return key[:4]+key[6:8]+key[4:6]+key[12:16]+key[8:12]
        self.assertEqual(lab.REVERSE_IDENTITY[:16],reverse(output_key4(lab.FORWARD)))
        self.assertEqual(output_key4(lab.REVERSE_FORWARD),reverse(lab.KEY))
        owner=object.__new__(lab.Lab);owner.prepared=True;owner.path=Path('fixture')
        owner.record=dict(session_touched=True,reverse_session_touched=True,changes=[],snapshots={})
        owner.save=lambda:None
        entries={};fail=[True]
        def call(kind,op='fetch',index=31,data=None):
            if kind=='snapshot':return {}
            raw=bytes.fromhex(data) if isinstance(data,str) else data or lab.IDENTITY
            key=raw[:16]
            if op=='insert':entries[key]=raw
            if op=='update' and key==lab.REVERSE_IDENTITY[:16] and fail[0]:
                raise RuntimeError('injected second direction update failure')
            if op=='delete':entries.pop(key,None)
            return dict(rc=0 if key in entries else 3,data=entries.get(key,raw).hex())
        owner.call=call
        with self.assertRaisesRegex(RuntimeError,'second direction'):owner.command('install')
        owner.restore();self.assertFalse(entries)
        fail[0]=False;owner.command('install');self.assertEqual(len(entries),2)
        owner.command('drop');self.assertEqual(set(entries.values()),{lab.DROP,lab.REVERSE_DROP})
        owner.command('remove');self.assertFalse(entries)

    def test_repeated_readiness_uses_existing_mapping_and_fresh_status(self):
        import ffn_fe100_packet_lab as lab
        import ffn_fe100_live_sessions as live
        with patch.dict(lab.WORKER_STATE,{},clear=True),patch.object(live,'LiveSessions') as factory:
            factory.return_value.status.side_effect=[{'ready':True},{'ready':False}]
            self.assertTrue(lab.worker({'kind':'readiness'},7)['ready'])
            self.assertFalse(lab.worker({'kind':'readiness'},7)['ready'])
            factory.assert_called_once_with(False,lock_fd=7,commissioning=True)

    def setUp(self):
        self.key=key4('198.18.0.1','198.18.0.2',49000,49001,17,4094)

    def test_native_cutthrough_nexthop(self):
        wire=forwarding_entry4(self.key,1001,31,decrement_ttl=True)
        native=encode_native(wire)
        self.assertEqual(native[32:40].hex(),'940000000000001f')
        self.assertEqual(native[52:56].hex(),'000003e9')
        self.assertEqual(decode_native(native,self.key),wire)

    def test_drop_roundtrip(self):
        wire=forwarding_entry4(self.key,1001,drop=True)
        self.assertEqual(wire[16:20].hex(),'08000000')
        self.assertEqual(decode_native(encode_native(wire),self.key),wire)

    def test_reject_unreviewed_flags_and_state(self):
        wire=forwarding_entry4(self.key,1001,31)
        for offset in (20,24,28,32,40,44,48,63):
            damaged=bytearray(wire);damaged[offset]|=1
            with self.assertRaises(ValueError):validate_entry4(damaged)
        for flag in (1<<29,1<<25,1<<23,1):
            damaged=bytearray(wire)
            struct.pack_into('!I',damaged,16,int.from_bytes(wire[16:20],'big')|flag)
            with self.assertRaises(ValueError):validate_entry4(damaged)

    def test_conflicting_actions(self):
        for kwargs in ({'drop':True,'next_hop':31},{'drop':True,'decrement_ttl':True},
                       {'next_hop':65536},{'next_hop':True},{'drop':1}):
            with self.assertRaises(ValueError):forwarding_entry4(self.key,1,**kwargs)


if __name__=='__main__':unittest.main()
