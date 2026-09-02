# FFN: capture what the chip puts in front of a frame it sends out a TM port.
#
# Port 4 (CP eth1) is tm_port_header_type_out=TM, so the switch BUILDS a TM
# header on egress. Reading that header is the one way to see the silicon's own
# destination encoding, rather than guessing at the 19-bit dst_prt field.
#
#   argv: <iface> <seconds>
import socket, sys, time, binascii

iface = sys.argv[1]
secs  = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
s.bind((iface, 0))
s.settimeout(1.0)

end = time.time() + secs
seen = 0
while time.time() < end and seen < 6:
    try:
        pkt = s.recv(2048)
    except Exception:
        continue
    seen += 1
    print("RX len=%d" % len(pkt))
    print("  hex: %s" % binascii.hexlify(pkt[:48]))
print("captured %d frame(s) on %s" % (seen, iface))
