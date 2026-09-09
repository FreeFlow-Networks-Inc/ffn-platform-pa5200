import json,socket,struct,subprocess as S,sys,time,threading,uuid,collections
sys.path.insert(0,'/usr/local/sbin')
from ffn_fabric import decode_cmh,encode_itmh
ssh=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','UserKnownHostsFile=/etc/ffn-ngfw/plane_boot_known_hosts','-o','ProxyCommand=ssh -F /etc/ffn-ngfw/ssh-cp.conf -W %h:%p ffn-cp','root@127.1.2.2']
state=json.loads(S.check_output(['/usr/local/sbin/ffn-network','status']))
macs={p['ifname']:bytes.fromhex(p['address'].replace(':','')) for p in state['interfaces'] if p['ifname'] in ('p5','p13')}
peer=bytes.fromhex('025220aabbdd')
S.run(ssh+['ip -n ffn-data neigh replace 198.18.1.2 lladdr 02:52:20:aa:bb:dd nud permanent dev p5'],check=True)
rx=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3));rx.bind(('enp8s0f1',0));rx.settimeout(.1)
rx.setsockopt(263,1,struct.pack('IHH8s',socket.if_nametoindex('enp8s0f1'),1,0,b''))
tx=socket.socket(socket.AF_PACKET,socket.SOCK_RAW);tx.bind(('enp8s0f0',0))
def csum(b):
 if len(b)%2:b+=b'\0'
 s=sum(struct.unpack('!%dH'%(len(b)//2),b))
 while s>>16:s=(s&65535)+(s>>16)
 return (~s)&65535
marker=uuid.uuid4().bytes
seen=collections.Counter();bad=[];stop=threading.Event()
def capture():
 while not stop.is_set():
  try:data,addr=rx.recvfrom(65535)
  except socket.timeout:continue
  if addr[2]==socket.PACKET_OUTGOING:continue
  item=decode_cmh(data)
  if not item:continue
  port,f=item
  if marker not in f or f[22]!=63:continue
  seq=struct.unpack('!I',f[58:62])[0]
  if port!=13 or f[:6]!=peer or f[6:12]!=macs['p5'] or csum(f[14:34]) or f[62:126]!=bytes(range(64)):bad.append(seq)
  seen[seq]+=1
thread=threading.Thread(target=capture);thread.start()
try:
 for seq in range(100):
  payload=marker+struct.pack('!I',seq)+bytes(range(64))
  udp=struct.pack('!HHHH',49000,49001,8+len(payload),0)+payload
  ip=struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(udp),seq,0x4000,64,17,0,socket.inet_aton('198.18.2.2'),socket.inet_aton('198.18.1.2'))
  ip=ip[:10]+struct.pack('!H',csum(ip))+ip[12:]
  tx.send(encode_itmh(5,macs['p13']+bytes.fromhex('025220aabbcc')+b'\x08\x00'+ip+udp))
  time.sleep(.02)
 end=time.monotonic()+10
 while len(seen)<100 and time.monotonic()<end:time.sleep(.1)
finally:
 stop.set();thread.join();rx.close();tx.close()
 S.run(ssh+['ip -n ffn-data neigh del 198.18.1.2 dev p5'],check=True)
result={'sent':100,'returned':len(seen),'missing':sum(i not in seen for i in range(100)),'duplicates':sum(n-1 for n in seen.values()),'corrupt':len(bad),'test':'physical FE100 to DP L3 and front-port egress','ttl':63}
print(json.dumps(result),flush=True)
assert not(result['missing'] or result['duplicates'] or result['corrupt']),result
