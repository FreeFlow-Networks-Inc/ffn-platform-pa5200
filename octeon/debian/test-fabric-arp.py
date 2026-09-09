import socket,struct,time,json,sys
sys.path.insert(0,'/usr/local/sbin')
from ffn_fabric import decode_cmh,encode_itmh
rx=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));rx.bind(('enp8s0f1',0));rx.settimeout(.5)
rx.setsockopt(263,1,struct.pack('IHH8s',socket.if_nametoindex('enp8s0f1'),1,0,b''))
tx=socket.socket(socket.AF_PACKET,socket.SOCK_RAW);tx.bind(('enp8s0f0',0))
peer=bytes.fromhex('025220aabbcc')
src=socket.inet_aton('198.18.2.2');dst=socket.inet_aton('198.18.2.1')
arp=struct.pack('!HHBBH',1,0x800,6,4,1)+peer+src+b'\0'*6+dst
frame=b'\xff'*6+peer+b'\x08\x06'+arp
for attempt in range(3):
 tx.send(encode_itmh(5,frame))
 end=time.monotonic()+3
 while time.monotonic()<end:
  try: data,addr=rx.recvfrom(65535)
  except socket.timeout:continue
  if addr[2]==socket.PACKET_OUTGOING:continue
  item=decode_cmh(data)
  if not item:continue
  port,f=item
  if f[12:14]==b'\x08\x06':print(json.dumps({'port':port,'frame':f.hex()}),flush=True)
  if port==5 and f[:6]==peer and f[20:22]==b'\0\x02' and f[28:32]==dst:
   print('PASS_PHYSICAL_DP_ARP',flush=True);sys.exit(0)
sys.exit('no DP ARP reply')
