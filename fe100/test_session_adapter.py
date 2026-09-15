import ctypes
import unittest
from ffn_fe100_sessions import key4, entry4, forwarding_entry4, SessionManager
from ffn_fe100_session_adapter import (
    NativeEntry, NativeSessionAdapter, OWNER_SHA256, encode_native, decode_native)


class Endpoint:
    def __init__(self):
        self.entries = {}
        self.calls = []
        self.fail_insert = 0
        self.initialized = True

    def status(self):
        return dict(owner_sha256=OWNER_SHA256, device=0, initialized=self.initialized,
                    exclusive=True, bounded=True)

    def call(self, op, native):
        key = native[16:32]
        self.calls.append((op, native))
        if op == 'fetch':
            return (0, self.entries[key]) if key in self.entries else (3, native)
        if op == 'insert':
            self.entries[key] = native
            if self.fail_insert and len(self.entries) == self.fail_insert:
                raise TimeoutError('accepted write, response lost')
        elif op == 'update':
            if key not in self.entries: raise RuntimeError('update without identity')
            self.entries[key] = native
        elif op == 'delete':
            assert native[52:56] == self.entries[key][52:56], 'delete flow ID'
            del self.entries[key]
        return 0, native


def healthy():
    return {'physical_transport_verified': True, 'summary': {
        'external_clock_status': {x:1 for x in
            ('dram_ddr_clk','dram_pclk','tcam_2x_clk','tcam_1x_clk')},
        'offload_verified': True}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.key = key4('192.0.2.1','192.0.2.2',1234,4321,17,9)
        self.reverse = key4('192.0.2.2','192.0.2.1',4321,1234,17,9)
        self.wire = entry4(self.key, 0x10203040)
        self.peer = entry4(self.reverse, 0x50607080)
        self.endpoint = Endpoint()
        self.adapter = NativeSessionAdapter(self.endpoint, healthy)

    def test_native_layout(self):
        native = encode_native(self.wire)
        self.assertEqual(ctypes.sizeof(NativeEntry),144)
        self.assertEqual(ctypes.alignment(NativeEntry),8)
        self.assertEqual(native[16:32],self.key)
        self.assertEqual(native[52:56],bytes.fromhex('10203040'))
        self.assertEqual(native[:16]+native[64:],bytes(96))
        self.assertEqual(decode_native(native,self.key),self.wire)

    def test_pair_install_and_delete_uses_flowid(self):
        manager = SessionManager(self.adapter)
        manager.install(20,[self.wire,self.peer],3)
        self.assertEqual(self.adapter.fetch(self.key),self.wire)
        manager.remove(20)
        self.assertFalse(self.endpoint.entries)
        self.assertFalse(manager.sessions)

    def test_ambiguous_second_write_reconciles_and_rolls_back(self):
        self.endpoint.fail_insert = 2
        manager = SessionManager(self.adapter)
        with self.assertRaises(TimeoutError):
            manager.install(20,[self.wire,self.peer],3)
        self.assertFalse(self.endpoint.entries)
        self.assertFalse(manager.sessions)

    def test_no_device_before_initialization(self):
        self.endpoint.initialized = False
        with self.assertRaises(RuntimeError): self.adapter.fetch(self.key)
        self.assertEqual(self.endpoint.calls,[])

    def test_wrong_key_and_nat_not_adopted(self):
        native = bytearray(encode_native(self.wire))
        native[32] |= 0x20
        with self.assertRaises(RuntimeError): decode_native(native,self.key)
        with self.assertRaises(RuntimeError): decode_native(encode_native(self.wire),self.reverse)

    def test_inactive_nat_payload_does_not_change_action(self):
        wire=forwarding_entry4(self.key,1,31)
        native=bytearray(encode_native(wire));native[64:100]=bytes(range(36))
        self.assertEqual(decode_native(native,self.key),wire)

    def test_hardware_error_not_absence(self):
        self.endpoint.call = lambda op, entry: (12,entry)
        with self.assertRaises(RuntimeError): self.adapter.fetch(self.key)

    def test_unqualified_no_insert(self):
        self.adapter.health = lambda: {'physical_transport_verified':False,'summary':{}}
        with self.assertRaises(RuntimeError): self.adapter.insert(self.wire)
        self.assertFalse(self.endpoint.calls)

    def test_action_install_uses_add_then_update(self):
        a=forwarding_entry4(self.key,1,31)
        b=forwarding_entry4(self.reverse,2,30,decrement_ttl=True)
        manager=SessionManager(self.adapter)
        manager.install(1,[a,b],1)
        mutations=[(op,native) for op,native in self.endpoint.calls if op!='fetch']
        self.assertEqual([op for op,_ in mutations],['insert','update','insert','update'])
        self.assertEqual(mutations[0][1][32:52],bytes(20))
        self.assertEqual(self.adapter.fetch(self.key),a)
        manager.remove(1)
        self.assertFalse(self.endpoint.entries)

    def test_update_failure_removes_intermediate_identity(self):
        a=forwarding_entry4(self.key,1,31);b=forwarding_entry4(self.reverse,2,30)
        call=self.endpoint.call
        def fail(op,native):
            if op=='update':raise TimeoutError('update interrupted before acceptance')
            return call(op,native)
        self.endpoint.call=fail
        manager=SessionManager(self.adapter)
        with self.assertRaises(TimeoutError):manager.install(1,[a,b],1)
        self.assertFalse(self.endpoint.entries)
        self.assertFalse(manager.sessions)

    def test_restart_reconciles_intermediate_identity(self):
        a=forwarding_entry4(self.key,1,31);b=forwarding_entry4(self.reverse,2,30)
        self.endpoint.entries[self.key]=encode_native(entry4(self.key,1))
        manager=SessionManager(self.adapter)
        manager.sessions={1:{'entries':(a,b),'revision':1,'state':'installing'}}
        manager.recovery_required=True
        manager.recover()
        self.assertFalse(self.endpoint.entries)
        self.assertFalse(manager.recovery_required)


if __name__ == '__main__': unittest.main()
