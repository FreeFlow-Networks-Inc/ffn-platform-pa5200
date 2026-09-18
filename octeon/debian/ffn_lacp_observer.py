#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded passive LACP sample on the commissioned OCTEON packet trunk.

No transmit, redirect, namespace, link or register changes. The packet owner
continues running. Empty observations do not prove absence of a physical peer.
"""
import json
from pathlib import Path
import socket
import time
from ffn_dp_packet_transport import decode_otmh_ssp,FRONT,validate_trunk
from ffn_lacp_packets import Observations


def sample(seconds=1.0,limit=4096):
    validate_trunk('ffnpkt0')
    observed=Observations();frames=0;outgoing=0;started=time.monotonic()
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as wire:
        wire.bind(('ffnpkt0',0))
        while frames<limit:
            left=seconds-(time.monotonic()-started)
            if left<=0:break
            wire.settimeout(left)
            try:raw,address=wire.recvfrom(4096)
            except socket.timeout:break
            frames+=1
            if address[2]==socket.PACKET_OUTGOING:outgoing+=1;continue
            decoded=decode_otmh_ssp(raw,set(FRONT))
            if decoded is None:continue
            port,frame=decoded
            if frame[12:14]==b'\x88\x09':observed.receive(port,frame,time.monotonic())
    return dict(observed.snapshot(time.monotonic()),boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                sampled_at=time.time(),sample_seconds=time.monotonic()-started,
                frames=frames,outgoing_ignored=outgoing,truncated=frames>=limit,
                transport='bcm-otmh-ssp',available=True,
                note='Only frames already delivered to the DP trunk can be observed; no LACP transmission or negotiation')


if __name__=='__main__':
    try:print(json.dumps(sample()))
    except (OSError,ValueError) as error:
        print(json.dumps(dict(available=False,observation_only=True,ports=[],error=str(error))))
