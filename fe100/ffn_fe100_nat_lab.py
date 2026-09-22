"""Fixed benchmark fixtures for the isolated 5/13 commissioning loop only.

Production translations always come from the DP session feed, never here.
"""
import socket
import struct


def tuples(mode,front5=False):
    if mode not in ('address','port'):raise ValueError('Unknown NAT lab mode')
    original=dict(source='198.18.0.1',destination='198.18.0.2',source_port=49000,destination_port=49001)
    translated=dict(original,source='198.19.0.1',source_port=50000 if mode=='port' else 49000)
    def reverse(t):return dict(source=t['destination'],destination=t['source'],source_port=t['destination_port'],destination_port=t['source_port'])
    return (reverse(translated),reverse(original)) if front5 else (original,translated)


def rewrite(frame,tuple_):
    from validate_physical_sessions import checksum
    # This expected-frame oracle deliberately recomputes full checksums; it
    # does not share FE100's incremental checksum/native action encoding.
    if frame[12:14]!=b'\x08\x00' or frame[14]!=0x45 or frame[23] not in (6,17):
        raise ValueError('Only fixed untagged IPv4 TCP/UDP probes are supported')
    raw=bytearray(frame)
    raw[26:30]=socket.inet_aton(tuple_['source']);raw[30:34]=socket.inet_aton(tuple_['destination'])
    struct.pack_into('!HH',raw,34,tuple_['source_port'],tuple_['destination_port'])
    check=50 if raw[23]==6 else 40
    raw[24:26]=bytes(2);raw[check:check+2]=bytes(2)
    struct.pack_into('!H',raw,24,checksum(bytes(raw[14:34])))
    pseudo=bytes(raw[26:34])+struct.pack('!BBH',0,raw[23],len(raw)-34)
    value=checksum(pseudo+bytes(raw[34:]))
    struct.pack_into('!H',raw,check,value if raw[23]==6 else value or 65535)
    return bytes(raw)


def probe_frames(token,count,protocol='udp'):
    from validate_physical_sessions import frames
    if protocol not in ('udp','tcp'):raise ValueError('Unknown probe protocol')
    packets=frames(token,count)
    if protocol=='udp':return packets
    result=[]
    original,_=tuples('address')
    for seq,packet in enumerate(packets):
        # Fixed ACK+PSH segments test rewrite/checksums, not TCP establishment
        # or retransmission tracking. Those remain production admission gates.
        tcp=struct.pack('!HHIIHHHH',49000,49001,0x12340000+seq,0x56780000,0x5018,65535,0,0)+packet[42:]
        raw=bytearray(packet[:34]+tcp);raw[23]=6
        struct.pack_into('!H',raw,16,len(raw)-14)
        result.append(rewrite(bytes(raw),original))
    return result
