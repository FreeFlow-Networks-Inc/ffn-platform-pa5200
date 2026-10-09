"""Root-only, bounded local CP policy-owner RPC. No admission operation."""
import json
from pathlib import Path
import socket
import stat
import struct
import uuid

SOCKET=Path('/run/ffn-fe100-control.sock')
MAX_MESSAGE=65536


def root_peer(sock):
    _,uid,_=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    if uid!=0:raise PermissionError('CP control requires a root peer')


def receive(sock):
    raw,_,flags,_=sock.recvmsg(MAX_MESSAGE)
    if not raw or flags & socket.MSG_TRUNC:raise ValueError('empty or oversized control message')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('control envelope must be an object')
    return value


def send(sock,value):
    raw=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>MAX_MESSAGE:raise ValueError('control message exceeds limit')
    if sock.send(raw)!=len(raw):raise RuntimeError('control response was not delivered')


def request(operation,payload,path=SOCKET,timeout=28):
    if operation not in ('status','replace','reconcile','qualify','observe-start','observe-chunk','observe-close') or not isinstance(payload,dict):
        raise ValueError('unsupported control operation')
    info=Path(path).lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o077:
        raise PermissionError('CP control socket ownership or permissions changed')
    identity=str(uuid.uuid4())
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(timeout);sock.connect(str(path));root_peer(sock)
        send(sock,dict(schema=1,id=identity,operation=operation,payload=payload))
        result=receive(sock)
    if result.get('schema')!=1 or result.get('id')!=identity or type(result.get('ok')) is not bool:
        raise RuntimeError('invalid CP control acknowledgement')
    if result['ok'] is not True:raise RuntimeError(str(result.get('error','CP control failed'))[:512])
    if not isinstance(result.get('result'),dict):raise RuntimeError('missing CP control state')
    return result['result']
