# SPDX-License-Identifier: GPL-2.0-or-later
"""Member-specific LACP adapter for the existing OCTEON packet owner.

The owner supplies its nonblocking TX socket and an Engine whose gate driver
controls the real data path. Call receive before inspection/data delivery and
service on every event-loop tick. This module opens no socket, installs no BCM
redirect and acquires no second packet owner. Installation does not activate it.
Only the commissioned optical-port map and OTMH_SSP envelope are accepted.
"""
from ffn_dp_packet_transport import FRONT,decode_otmh_ssp,encode


class TrunkLACP:
    def __init__(self,engine,wire):
        if not set(engine.members)<=set(FRONT):
            raise ValueError('LACP members require a commissioned optical-port mapping')
        self.engine=engine;self.wire=wire;self.sent=0;self.consumed=0

    def receive(self,raw,now,*,outgoing=False):
        """True consumes a member slow-protocol frame, including invalid PDUs.

        False leaves the existing owner responsible for normal demultiplexing.
        Locally transmitted frames must already be excluded from its data path.
        """
        if outgoing:return False
        decoded=decode_otmh_ssp(raw,set(self.engine.members))
        if decoded is None:return False
        port,frame=decoded
        if frame[12:14]!=b'\x88\x09':return False
        self.consumed+=1
        self.engine.receive(port,frame,now)
        return True

    def service(self,now):
        """Send directly to each member, never through an aggregate hash.

        A short/failed send latches a fault and withdraws all gates; there is no
        retry of an ambiguous datagram or automatic restart after a fault.
        """
        for port,frame in self.engine.transmissions(now):
            try:
                packet=encode(port,frame)
                if self.wire.send(packet)!=len(packet):raise OSError('Short LACP trunk write')
                self.sent+=1
            except OSError as error:
                self.engine.abort('LACP member transmit failed: '+str(error),now)
                break
        return dict(sent=self.sent,consumed=self.consumed,fault=self.engine.fault)
