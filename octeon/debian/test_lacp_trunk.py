import struct
import sys
import unittest
from ffn_lacp_engine import Engine,encode,ZERO
from ffn_lacp_packets import decode

if sys.platform=='linux':
    from ffn_lacp_trunk import TrunkLACP

LOCAL='02:00:00:00:00:01'
REMOTE='02:00:00:00:00:02'


class Gates:
    def apply(self,value):self.state=value;return value


class Wire:
    def __init__(self):self.packets=[];self.failure=None
    def send(self,packet):
        if self.failure:raise self.failure
        self.packets.append(packet);return len(packet)


@unittest.skipUnless(sys.platform=='linux','OCTEON packet adapter requires Linux')
class TrunkTests(unittest.TestCase):
    def make(self):
        engine=Engine(LOCAL,1,{23:LOCAL,24:LOCAL},Gates())
        wire=Wire();adapter=TrunkLACP(engine,wire)
        for port in engine.members:engine.link(port,True,100000,0)
        return engine,wire,adapter

    def test_member_specific_transmission_is_network_byte_order(self):
        engine,wire,adapter=self.make();adapter.service(0)
        self.assertEqual(len(wire.packets),2)
        for packet,bcm,port in zip(wire.packets,(34,35),(23,24)):
            self.assertEqual(packet[:4],b'\x01'+struct.pack('!H',bcm)+b'\0')
            self.assertEqual(packet[16:24],bytes(8))
            pdu=decode(packet[4:16]+packet[24:])
            self.assertEqual(pdu['actor']['port'],port)
            self.assertFalse(pdu['actor']['flags']['collecting'])

    def test_only_correct_ingress_member_updates_peer(self):
        engine,wire,adapter=self.make()
        peer=dict(engine.actor(23),system=REMOTE,port=9)
        frame=encode(peer,ZERO,REMOTE);raw=b'\0\x18\0\x22'+frame
        self.assertTrue(adapter.receive(raw,0))
        self.assertEqual(engine.members[23]['peer']['port'],9)
        self.assertIsNone(engine.members[24]['peer'])
        self.assertFalse(adapter.receive(raw,0,outgoing=True))
        self.assertFalse(adapter.receive(b'\0\x18\0\x21'+frame,0))
        self.assertFalse(adapter.receive(b'\0\x19\0\x22'+frame,0))
        self.assertEqual(engine.members[23]['rx_count'],1)

    def test_invalid_slow_protocol_consumed_without_data_delivery(self):
        engine,wire,adapter=self.make()
        frame=b'\x01\x80\xc2\0\0\x02'+bytes(6)+b'\x88\x09'+bytes(10)
        self.assertTrue(adapter.receive(b'\0\x18\0\x22'+frame,0))
        self.assertEqual(engine.members[23]['invalid'],1)
        self.assertFalse(adapter.receive(b'\0\x18\0\x22'+frame[:12]+b'\x08\x00'+bytes(20),0))

    def test_send_failure_and_short_write_latch_and_withdraw(self):
        for short in (False,True):
            engine,wire,adapter=self.make()
            peer=Engine(REMOTE,1,{23:REMOTE,24:REMOTE},Gates())
            for step in range(100):
                now=step/10
                for actor in (engine,peer):
                    for port in actor.members:actor.link(port,True,100000,now)
                for source,target in ((engine,peer),(peer,engine)):
                    for port,frame in source.transmissions(now):target.receive(port,frame,now)
            self.assertEqual(engine.status(now)['distributing'],[23,24])
            if short:wire.send=lambda _:1
            else:wire.failure=BlockingIOError('queue full')
            result=adapter.service(11)
            self.assertIn('transmit failed',result['fault']);self.assertEqual(engine.applied,engine.closed())
            self.assertEqual(adapter.service(12)['sent'],0)

    def test_uncommissioned_copper_mapping_rejected(self):
        engine=Engine(LOCAL,1,{1:LOCAL},Gates())
        with self.assertRaises(ValueError):TrunkLACP(engine,Wire())


if __name__=='__main__':unittest.main()
