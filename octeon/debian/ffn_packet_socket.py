#!/usr/bin/env python3
"""Kernel socket admission for the commissioned raw OCTEON trunk.

This is an early demultiplexer, not firewall policy or a replacement decoder.
The packet owner still validates full envelopes, LACP gates and inspection.
"""
import ctypes
import socket

SOL_PACKET=263
PACKET_IGNORE_OUTGOING=23
SO_ATTACH_FILTER=26  # Linux UAPI, both MIPS and asm-generic.


class Instruction(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_=[('length',ctypes.c_ushort),('instructions',ctypes.POINTER(Instruction))]


def otmh_program(sources):
    sources=sorted(set(sources))
    if not sources or len(sources)>64 or any(type(p) is not int or not 0<=p<=65535 for p in sources):
        raise ValueError('One to 64 commissioned OTMH source IDs required')
    # BPF packet halfword loads are network endian, independent of CPU endian.
    # On an out-of-bounds load Linux drops the packet.
    code=[(0x28,0,0,0),                    # ldh [0]
          (0x15,0,len(sources)+1,24),      # fixed BCM return destination
          (0x28,0,0,2)]                   # ldh [2]: source system port
    for i,source in enumerate(sources):
        code.append((0x15,len(sources)-i,0,source))
    code.extend([(0x06,0,0,0),(0x06,0,0,65535)])
    return code


def configure_rx(sock,sources):
    rows=otmh_program(sources)
    instructions=(Instruction*len(rows))(*(Instruction(*row) for row in rows))
    program=Program(len(rows),instructions)
    # The kernel copies the instruction array before setsockopt returns.
    sock.setsockopt(socket.SOL_SOCKET,SO_ATTACH_FILTER,bytes(program))
    sock.setsockopt(SOL_PACKET,PACKET_IGNORE_OUTGOING,1)
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4*1024*1024)


def tx_socket(interface):
    # Protocol zero prevents a transmit-only descriptor accumulating RX copies.
    sock=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,0)
    try:sock.bind((interface,0))
    except BaseException:
        sock.close()
        raise
    return sock
