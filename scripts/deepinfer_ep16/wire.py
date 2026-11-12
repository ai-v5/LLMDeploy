"""Typed JSON frames for task-local worker control; no pickle or code messages."""
import base64
import json
import socket
import struct
import threading

MAX_FRAME=64*1024*1024
def pack(value):
    if value is None or type(value) in (str,int,float,bool):return value
    if isinstance(value,bytes):return {'t':'bytes','v':base64.b64encode(value).decode('ascii')}
    if isinstance(value,tuple):return {'t':'tuple','v':[pack(v) for v in value]}
    if isinstance(value,list):return [pack(v) for v in value]
    if isinstance(value,dict):return {'t':'dict','v':[[pack(k),pack(v)] for k,v in value.items()]}
    raise TypeError('Unsupported worker wire type: '+type(value).__name__)
def unpack(value):
    if isinstance(value,list):return [unpack(v) for v in value]
    if isinstance(value,dict):
        if set(value)!={'t','v'}:raise ValueError('Malformed wire object')
        if value['t']=='bytes':return base64.b64decode(value['v'],validate=True)
        if value['t']=='tuple':return tuple(unpack(v) for v in value['v'])
        if value['t']=='dict':return {unpack(k):unpack(v) for k,v in value['v']}
        raise ValueError('Unknown wire type')
    return value
class Channel:
    def __init__(self,sock):
        self.sock=sock;self.lock=threading.Lock()
        if sock.family in (socket.AF_INET,socket.AF_INET6):sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    def send(self,value):
        raw=json.dumps(pack(value),ensure_ascii=True,allow_nan=False,separators=(',',':')).encode('ascii')
        if len(raw)>MAX_FRAME:raise ValueError('Worker frame exceeds64MiB')
        with self.lock:self.sock.sendall(struct.pack('!I',len(raw))+raw)
    def exact(self,count):
        chunks=[]
        while count:
            data=self.sock.recv(min(count,1024*1024))
            if not data:raise EOFError('Worker control connection closed')
            chunks.append(data);count-=len(data)
        return b''.join(chunks)
    def receive(self):
        size=struct.unpack('!I',self.exact(4))[0]
        if not 0<size<=MAX_FRAME:raise ValueError('Invalid worker frame length')
        return unpack(json.loads(self.exact(size)))
    def close(self):
        try:self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.sock.close()
