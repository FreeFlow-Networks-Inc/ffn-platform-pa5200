# FFN: send a frame prefixed with a real ITMH out a TM-header ingress port.
#
# Port 5 is tm_port_header_type_in=TM, so the chip parses the first 4 bytes of
# every frame as an Ingress TM Header and takes the destination from it. Plain
# Ethernet therefore had its destination MAC eaten as an ITMH -- ff:ff:ff:ff
# gave snoop=15, which is exactly the bcmRxTrapItmhSnoop15 the chip reported.
#
# Layout recovered from the vendor's own DWARF (struct dune_itmh_v3_s, 4 bytes,
# in libpandp_cp.so):
#     [31:30] type  [29] mirr_dis  [28:27] dp  [26:8] dst_prt
#     [7:4] snoop   [3:1] tclass   [0] ext
#
#   argv: <iface> <itmh_hex> <count> [tag]
import socket, struct, sys

iface = sys.argv[1]
itmh  = int(sys.argv[2], 16)
count = int(sys.argv[3])
tag   = sys.argv[4] if len(sys.argv) > 4 else "X"

src  = open("/sys/class/net/%s/address" % iface).read().strip()
srcb = "".join(chr(int(x, 16)) for x in src.split(":"))

frame = (struct.pack("!I", itmh)
         + "\xff" * 6 + srcb + struct.pack("!H", 0x0800)
         + ("FFN-ITMH-" + tag).ljust(46, "\x00"))

s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.bind((iface, 0))
for _ in range(count):
    s.send(frame)
print("sent %d itmh=0x%08x tag=%s" % (count, itmh, tag))
