#!/usr/bin/env python3
"""Exercise the real socket ABI in a disposable Linux network namespace.

Run as root on both build host and OCTEON. No production device is touched.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
from ffn_packet_socket import configure_rx, tx_socket


def run():
    subprocess.run(['ip', 'link', 'add', 'testa', 'type', 'veth', 'peer', 'name', 'testb'], check=True)
    for name in ('testa', 'testb'):
        subprocess.run(['ip', 'link', 'set', name, 'up'], check=True)
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as receiver, tx_socket('testa') as sender:
        receiver.bind(('testb', 0))
        configure_rx(receiver, [28, 0x8001, 0x8101])
        receiver.settimeout(0.15)
        sender.settimeout(0.15)
        for source in (28, 0x8001, 0x8101, 29, 0x8201):
            frame = b'\0\x18' + source.to_bytes(2, 'big') + b'\0'*8 + b'\x88\xb5' + bytes(range(50))
            assert sender.send(frame) == len(frame)
            if source in (28, 0x8001, 0x8101):
                assert receiver.recv(2048) == frame, 'Frame changed in transport'
            else:
                try:
                    receiver.recv(2048)
                except socket.timeout:
                    pass
                else:
                    raise AssertionError('Uncommissioned source was admitted')
        # A second socket transmits an otherwise accepted envelope on testb.
        # The receive owner must not receive its outgoing copy, and protocol-zero
        # TX on testa must not accumulate the incoming copy either.
        frame = b'\0\x18\0\x1c' + b'\0'*8 + b'\x88\xb5' + bytes(range(50))
        with tx_socket('testb') as outgoing:
            outgoing.send(frame)
        for descriptor in (receiver, sender):
            try:
                descriptor.recv(2048)
            except socket.timeout:
                pass
            else:
                raise AssertionError('Unwanted packet copy received')
    print('PASS kernel packet filter, LAG aliases, exact transmit, outgoing exclusion, TX-only RX exclusion; machine=' + os.uname().machine)


if __name__ == '__main__':
    if sys.argv[1:] == ['--inside']:
        run()
    else:
        subprocess.run(['unshare', '--net', sys.executable, str(Path(__file__).resolve()), '--inside'], check=True)
