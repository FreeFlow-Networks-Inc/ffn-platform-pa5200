import unittest
from ffn_aggregate_datapath import Gates,flow_key
from test_lacp_engine import engine,run


class AggregateDatapathTests(unittest.TestCase):
    def test_gate_readback_is_real_packet_permission_and_independent_copy(self):
        gate=Gates((23,24));delivered=[];sent=[];frame=bytes.fromhex('0200000000010200000000020806')+bytes(46)
        self.assertFalse(gate.receive(23,frame,lambda *v:delivered.append(v)))
        self.assertIsNone(gate.transmit(frame,lambda *v:sent.append(v)))
        value={23:dict(collect=True,distribute=True),24:dict(collect=False,distribute=False)}
        readback=gate.apply(value);value[24]['collect']=True;readback[24]['distribute']=True
        self.assertFalse(gate.receive(24,frame,lambda *v:delivered.append(v)))
        self.assertTrue(gate.receive(23,frame,lambda *v:delivered.append(v)))
        self.assertEqual(gate.transmit(frame,lambda *v:sent.append(v)),23)
        self.assertEqual(len(sent),1);self.assertEqual(len(delivered),1)
        gate.apply({p:dict(collect=False,distribute=False) for p in (23,24)})
        self.assertIsNone(gate.transmit(frame,lambda *v:sent.append(v)))

    def test_rendezvous_hash_and_fragment_consistency(self):
        gate=Gates((23,24));gate.apply({p:dict(collect=True,distribute=True) for p in (23,24)})
        first=bytearray(bytes.fromhex('0200000000010200000000020800450000280000200040110000c0000201c0000202')+bytes(26))
        second=bytearray(first);second[20:22]=b'\0\x01';second[34:]=bytes([42])*26
        self.assertEqual(flow_key(first),flow_key(second))
        self.assertEqual(gate.transmit(first,lambda *v:None),gate.transmit(second,lambda *v:None))
        gate.apply({23:dict(collect=False,distribute=False),24:dict(collect=True,distribute=True)})
        self.assertEqual(gate.transmit(first,lambda *v:None),24)

    def test_control_only_owner_never_advertises_or_allows_collection(self):
        a=engine(collecting=False);b=engine('02:00:00:00:00:02');now=run(a,b)
        self.assertTrue(a.members[1]['matched']);self.assertTrue(a.members[1]['selected'])
        self.assertEqual(a.status(now)['distributing'],[])
        self.assertEqual(a.applied,a.closed())
        self.assertEqual(a.actor(1)['state']&48,0)


if __name__=='__main__':unittest.main()
