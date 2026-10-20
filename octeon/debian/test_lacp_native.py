# SPDX-License-Identifier: GPL-2.0-or-later
"""The native codec against the Python codec, byte for byte, plus the shim's views.

Needs FFN_LACP_LIB (libffn-lacp.so built from octeon/native); skipped otherwise.
"""
import os
import unittest
import ffn_lacp_engine as reference
import ffn_lacp_packets as packets

if os.environ.get('FFN_LACP_LIB') and os.path.exists(os.environ['FFN_LACP_LIB']):
    import ffn_lacp_native as native
else:native=None

ACTOR=dict(system_priority=32768,system='02:00:00:00:00:01',key=7,port_priority=32768,port=3,state=0x3f)
PARTNER=dict(system_priority=1,system='02:00:00:00:00:02',key=100,port_priority=2,port=202,state=0x07)


@unittest.skipIf(native is None,'FFN_LACP_LIB not available')
class CodecTests(unittest.TestCase):
    def test_encode_matches_python_byte_for_byte(self):
        for partner in (PARTNER,reference.ZERO):
            self.assertEqual(native.encode(ACTOR,partner,'02:00:00:00:00:01'),reference.encode(ACTOR,partner,'02:00:00:00:00:01'))

    def test_decode_matches_python_including_flags(self):
        frame=reference.encode(ACTOR,PARTNER,'02:00:00:00:00:09')
        self.assertEqual(native.decode(frame),packets.decode(frame))

    def test_both_codecs_reject_the_same_frames(self):
        good=reference.encode(ACTOR,PARTNER,'02:00:00:00:00:09')
        broken=[good[:123],b'\x03'+good[1:],good[:6]+b'\x01'+good[7:],good[:15]+b'\x02'+good[16:],
                good[:17]+b'\x13'+good[18:],good[:31]+b'\x00'+good[32:],good[:20]+bytes(6)+good[26:],
                good[:73]+b'\x01'+good[74:],good[:57]+b'\x11'+good[58:]]
        for frame in broken:
            with self.assertRaises(ValueError):packets.decode(frame)
            with self.assertRaises(ValueError):native.decode(frame)
        for source in ('01:00:00:00:00:01','00:00:00:00:00:00'):
            with self.assertRaises(ValueError):native.encode(ACTOR,PARTNER,source)
            with self.assertRaises(ValueError):reference.encode(ACTOR,PARTNER,source)

    def test_member_view_and_time(self):
        class Gates:
            def apply(self,value):return value
        engine=native.Engine('02:00:00:00:00:01',1,{5:'02:00:00:00:00:01',2:'02:00:00:00:00:01'},Gates())
        self.assertEqual(list(engine.members),[5,2]);self.assertIsNone(engine.now)
        engine.link(5,True,10000,1.5)
        self.assertEqual(engine.now,1.5);self.assertEqual(engine.members[5]['rx'],'expired');self.assertTrue(engine.members[5]['link'])
        self.assertAlmostEqual(engine.members[5]['lease'],4.5);self.assertEqual(engine.members[2]['rx'],'disabled')
        self.assertEqual(engine.applied,engine.closed());self.assertEqual(engine.status(1.6)['distributing'],[])
        with self.assertRaises(ValueError):engine.tick(1.4)
        with self.assertRaises(TypeError):engine.members[5]['rx']='current'
        with self.assertRaises(KeyError):engine.members[9]


if __name__=='__main__':unittest.main()
