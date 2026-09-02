# FFN: determine WHERE the TM header sits, using the snoop field as a probe.
#
# All previous evidence for "ITMH at offset 0" was non-discriminating: the test
# frames began ff:ff:ff:ff:ff:ff, so a header read at offset 0, 1 or 2 would all
# see 0xffffffff and report snoop=15 either way.
#
# So plant DIFFERENT snoop nibbles at the candidate offsets and let the chip say
# which one it read. snoop is bits [7:4] of the 4th header byte, so:
#
#     header at offset 0  ->  snoop comes from byte 3
#     header at offset 2  ->  snoop comes from byte 5
#     header at offset 4  ->  snoop comes from byte 7
#
# Byte 3 = 0x30, byte 5 = 0x70, byte 7 = 0xb0, everything else 0. The reported
# bcmRxTrapItmhSnoopN then names the offset outright: 3 -> 0, 7 -> 2, 11 -> 4.
import socket, struct, sys

iface = sys.argv[1]
count = int(sys.argv[2]) if len(sys.argv) > 2 else 50

src  = open("/sys/class/net/%s/address" % iface).read().strip()
srcb = "".join(chr(int(x, 16)) for x in src.split(":"))

probe = "\x00\x00\x00\x30\x00\x70\x00\xb0"
frame = probe + "\xff" * 6 + srcb + struct.pack("!H", 0x0800) + "FFN-OFFSET".ljust(46, "\x00")

s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.bind((iface, 0))
for _ in range(count):
    s.send(frame)
print("sent %d offset probes: snoop 3 => hdr@0, 7 => hdr@2, 11 => hdr@4" % count)
