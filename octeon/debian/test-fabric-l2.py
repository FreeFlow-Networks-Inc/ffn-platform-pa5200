import socket,struct,time,json,sys,uuid,collections,threading
sys.path.insert(0,'/usr/local/sbin')
from ffn_fabric import decode_cmh,encode_itmh
rx=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));rx.bind(('enp8s0f1',0));rx.settimeout(.1)
rx.setsockopt(263,1,struct.pack('IHH8s',socket.if_nametoindex('enp8s0f1'),1,0,b''))
tx=socket.socket(socket.AF_PACKET,socket.SOCK_RAW);tx.bind(('enp8s0f0',0))
marker=uuid.uuid4().bytes;seen=collections.Counter();bad=[];stop=threading.Event()
frames={i:b'\xff'*6+bytes.fromhex('025220abcd01')+b'\x88\xb5'+marker+struct.pack('!I',i)+bytes(range(192)) for i in range(100)}
def capture():
 while not stop.is_set():
  try:data,addr=rx.recvfrom(65535)
  except socket.timeout:continue
  if addr[2]==socket.PACKET_OUTGOING:continue
  item=decode_cmh(data)
  if not item:continue
  port,f=item
  if port!=13 or marker not in f:continue
  seq=struct.unpack('!I',f[30:34])[0]
  if f!=frames.get(seq):bad.append(seq)
  seen[seq]+=1
thread=threading.Thread(target=capture);thread.start()
try:
 for seq,frame in frames.items():
  tx.send(encode_itmh(3,frame));time.sleep(.02)
 end=time.monotonic()+10
 while len(seen)<100 and time.monotonic()<end:time.sleep(.1)
finally:
 stop.set();thread.join();rx.close();tx.close()
r={'test':'front3 cable front1 FE100 DP bridge front5 cable front13','sent':100,'returned':len(seen),'missing':sum(i not in seen for i in range(100)),'duplicates':sum(n-1 for n in seen.values()),'corrupt':len(bad)}
print(json.dumps(r));assert not(r['missing'] or r['duplicates'] or r['corrupt'])
