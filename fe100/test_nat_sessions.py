import unittest
from ffn_fe100_nat import session_pair4
from ffn_fe100_sessions import nat_entry4, output_key4, validate_entry4, SessionManager
from ffn_fe100_session_adapter import encode_native, decode_native, NativeSessionAdapter
from test_session_adapter import Endpoint, healthy
from test_sessions import Backend


def row(src='192.0.2.2',dst='198.51.100.8',sport=1234,dport=443,proto=17):
    return dict(source=src,destination=dst,source_port=sport,destination_port=dport,protocol=proto)


def reverse(r):
    return row(r['destination'],r['source'],r['destination_port'],r['source_port'],r['protocol'])


class NatSessions(unittest.TestCase):
    def test_ipv6_translation_cannot_use_ipv4_offload_codec(self):
        from ffn_fe100_nat import capabilities
        for kind in ('nat64','nptv6'):
            self.assertFalse(capabilities()['translation_types'][kind]['production_admission'])
            self.assertFalse(capabilities()['translation_types'][kind]['codec'])
        with self.assertRaises(ValueError):session_pair4(1,row(src='2001:db8::1'),row(),9,[31,30])

    def setUp(self):
        self.original=row();self.reply=row('198.51.100.8','203.0.113.9',443,45000)
        self.entries=session_pair4(42,self.original,self.reply,9,[31,30])

    def test_reference_native_and_wire_vectors_are_distinct(self):
        forward=self.entries[0]
        self.assertEqual(forward[:24].hex(),'4011000904d201bbc0000202c6336408d40000000000001f')
        self.assertEqual(forward[36:40].hex(),'00000054')
        self.assertEqual(forward[48:].hex(),'cb007109c6336408afc801bb00000000')
        native=encode_native(forward)
        self.assertEqual(native[64:76].hex(),'afc801bbcb007109c6336408')
        self.assertEqual(native[76:],bytes(68))
        self.assertEqual(decode_native(native,forward[:16]),forward)
        self.assertEqual(validate_entry4(self.entries[1]),self.entries[1])

    def test_snat_dnat_combined_and_port_only_pairs(self):
        translations=[row('203.0.113.9',sport=45000),
                      row(dst='203.0.113.10',dport=8443),
                      row('203.0.113.9','203.0.113.10',45000,8443),
                      row(sport=45000),row(dport=8443)]
        for proto in (6,17):
            for target in translations:
                original=dict(self.original,protocol=proto);target=dict(target,protocol=proto)
                entries=session_pair4(42,original,reverse(target),9,[31,30])
                backend=Backend();manager=SessionManager(backend)
                manager.install(42,entries,7)
                for entry in entries:self.assertEqual(decode_native(encode_native(entry),entry[:16]),entry)
                manager.remove(42);self.assertFalse(backend.rows)

    def test_address_only_uses_nat_without_port_rewrite(self):
        entries=session_pair4(42,self.original,reverse(row('203.0.113.9')),9,[31,30])
        for entry in entries:self.assertEqual((int.from_bytes(entry[16:20],'big')>>29)&3,1)

    def test_untranslated_pair_remains_plain_routed_forwarding(self):
        entries=session_pair4(42,self.original,reverse(self.original),9,[31,30])
        manager=SessionManager(Backend());manager.install(42,entries,7)
        for entry in entries:
            self.assertEqual(entry[48:],bytes(16))
            self.assertEqual(int.from_bytes(entry[16:20],'big')&(3<<29),0)
            self.assertTrue(int.from_bytes(entry[16:20],'big')&(1<<28))

    def test_one_sided_or_mismatched_translation_rejected_before_writes(self):
        other=session_pair4(43,self.original,reverse(row('203.0.113.10',sport=45001)),9,[31,30])
        backend=Backend();manager=SessionManager(backend)
        with self.assertRaises(ValueError):manager.install(42,[self.entries[0],other[1]],7)
        self.assertEqual(backend.writes,0)

    def test_nat_requires_independent_packet_qualification(self):
        endpoint=Endpoint();adapter=NativeSessionAdapter(endpoint,healthy)
        with self.assertRaisesRegex(RuntimeError,'NAT packet'):adapter.insert(self.entries[0])
        self.assertFalse(endpoint.calls)
        adapter.health=lambda:dict(healthy(),nat_offload_verified=True)
        manager=SessionManager(adapter);manager.install(42,self.entries,7)
        self.assertEqual([op for op,_ in endpoint.calls if op!='fetch'],['insert','update','insert','update'])
        manager.remove(42);self.assertFalse(endpoint.entries)

    def test_ambiguous_nat_action_update_rolls_back_both_directions(self):
        endpoint=Endpoint();real=endpoint.call;updates=0
        def lose_reply(op,native):
            nonlocal updates
            result=real(op,native)
            if op=='update':
                updates+=1
                if updates==2:raise TimeoutError('update accepted; reply lost')
            return result
        endpoint.call=lose_reply
        manager=SessionManager(NativeSessionAdapter(endpoint,lambda:dict(healthy(),nat_offload_verified=True)))
        with self.assertRaises(TimeoutError):manager.install(42,self.entries,7)
        self.assertFalse(endpoint.entries);self.assertFalse(manager.sessions)

    def test_inactive_union_tail_ignored_but_active_translation_preserved(self):
        wire=self.entries[0];native=bytearray(encode_native(wire));native[76:100]=bytes(range(24))
        self.assertEqual(decode_native(native,wire[:16]),wire)
        native[68:72]=bytes.fromhex('cb00710a')
        self.assertNotEqual(decode_native(native,wire[:16]),wire)

    def test_malformed_unsupported_and_noop_actions_rejected(self):
        for offset,value in ((16,0xf4),(40,1),(60,1)):
            wire=bytearray(self.entries[0]);wire[offset]=value
            with self.assertRaises(ValueError):validate_entry4(wire)
        for original in (dict(self.original,protocol=1),dict(self.original,source_port=True),
                         dict(self.original,source='::1'),dict(self.original,extra=1)):
            with self.assertRaises(ValueError):session_pair4(42,original,self.reply,9,[31,30])
        with self.assertRaises(ValueError):
            nat_entry4(self.entries[0][:16],1,31,{k:v for k,v in self.original.items() if k!='protocol'})


if __name__=='__main__':unittest.main()
