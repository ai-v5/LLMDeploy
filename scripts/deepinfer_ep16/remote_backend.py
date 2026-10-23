"""Extend existing EnginePool dispatch with eight local and eight peer workers."""
from dataclasses import asdict
import json
import multiprocessing as mp
import os
import queue
import socket
import subprocess
import threading
import time
from recipe_api.backend import EnginePool,worker
from wire import Channel

class Peer:
    def __init__(self,events,config,bind,peer,run_id):
        self.events=events;self.config=config;self.peer=peer;self.run_id=run_id
        self.outgoing=queue.Queue();self.connected=threading.Event();self.stopped=threading.Event()
        self.failed=False;self.closing=False;self.channel=None
        self.live_ranks=set()
        self.listener=socket.socket();self.listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.listener.bind(bind);self.listener.listen(1);self.listener.settimeout(1)
        self.owners={}
        self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
    def fail(self,error):
        if not self.closing and not self.failed:
            self.failed=True;self.events.put(('error',8,'Peer control failed: '+str(error)))
        self.stopped.set()
        if self.channel:self.channel.close()
    def write(self):
        try:
            while True:
                value=self.outgoing.get()
                if value is None:return
                self.channel.send(value)
        except BaseException as error:self.fail(error)
    def serve(self):
        try:
            while not self.closing:
                try:sock,address=self.listener.accept();break
                except socket.timeout:continue
            else:return
            if address[0]!=self.peer:sock.close();raise ValueError('Unexpected worker peer address')
            sock.settimeout(30);self.channel=Channel(sock)
            hello=self.channel.receive()
            if hello!={'kind':'hello','version':1,'run_id':self.run_id,'ranks':list(range(8,16))}:raise ValueError('Wrong worker world')
            self.channel.send({'kind':'config','config':asdict(self.config),'run_id':self.run_id})
            sock.settimeout(None);self.connected.set()
            threading.Thread(target=self.write,daemon=True).start()
            while True:
                message=self.channel.receive()
                if message['kind']=='started':
                    rank=message['rank']
                    if rank not in range(8,16) or rank in self.live_ranks:raise ValueError('Bad worker start acknowledgement')
                    self.live_ranks.add(rank);continue
                if message['kind']=='stopped':
                    if not self.closing:raise RuntimeError('Remote workers stopped before pool close')
                    self.stopped.set();return
                if message['kind']=='exit':raise RuntimeError('Remote worker exited: '+str(message['rank']))
                if message['kind']!='event':raise ValueError('Unknown peer message')
                rank,event=message['rank'],message['event']
                if rank not in range(8,16) or not isinstance(event,tuple) or len(event)!=3:raise ValueError('Bad worker event')
                kind,identifier,detail=event
                if kind in ('ready','cache','idle','error'):
                    if identifier!=rank:raise ValueError('Peer rank mismatch')
                elif kind in ('prompt','tokens','eos','finished','cancelled'):
                    if self.owners.get(identifier)!=rank:raise ValueError('Peer request owner mismatch')
                    if kind in ('finished','cancelled'):self.owners.pop(identifier)
                else:raise ValueError('Unsupported peer event')
                self.events.put(event)
        except BaseException as error:self.fail(error)
        finally:
            self.listener.close()
    def put(self,rank,message):
        if self.failed or self.closing:raise RuntimeError('Peer workers unavailable')
        if message is not None and message[0]=='add':
            if message[1] in self.owners:raise ValueError('Duplicate peer request')
            self.owners[message[1]]=rank
        self.outgoing.put({'kind':'job','rank':rank,'message':message})
    def close(self):
        if self.closing:return
        self.closing=True
        if self.connected.is_set() and not self.failed:
            self.outgoing.put({'kind':'stop'})
        else:self.stopped.set()
    def finish(self,timeout):
        self.stopped.wait(timeout)
        self.outgoing.put(None)
        if self.channel:self.channel.close()
        self.listener.close()

class RemoteJobs:
    def __init__(self,peer,rank):self.peer=peer;self.rank=rank
    def put(self,message):self.peer.put(self.rank,message)
class RemoteProcess:
    def __init__(self,peer,rank):self.peer=peer;self.rank=rank;self.started=False
    def start(self):self.started=True
    def is_alive(self):return self.started and self.rank in self.peer.live_ranks and not self.peer.failed and not self.peer.stopped.is_set()

class RemoteEnginePool(EnginePool):
    def __init__(self,config,worker_python,*,bind,peer,run_id,worker_target=worker):
        if (config.ep,config.dp)!=(16,16):raise ValueError('This task adapter requires EP16/DP16 and two eight-worker nodes')
        mp.set_executable(worker_python);context=mp.get_context('spawn')
        python_path=json.loads(subprocess.check_output([worker_python,'-c','import json,sys;print(json.dumps(sys.path))'],text=True))
        self.events=context.Queue();self.cancel=context.Event()
        self.config=config;self.failed=False;self.ready=False;self.next_rank=self.counter=0
        self.peer=Peer(self.events,config,bind,peer,run_id)
        self.jobs=[context.Queue() for _ in range(8)]+[RemoteJobs(self.peer,i) for i in range(8,16)]
        self.local_processes=[context.Process(target=worker_target,args=(rank,config,self.jobs[rank],self.events,self.cancel,python_path)) for rank in range(8)]
        self.processes=self.local_processes+[RemoteProcess(self.peer,i) for i in range(8,16)]
        self._closed=False;self._init_dispatch()
    def start(self):
        try:return super().start()
        except BaseException:self.close();raise
    def available(self):return self.peer.connected.is_set() and super().available()
    def receive(self,deadline=None):
        while True:
            try:return self.events.get(timeout=1)
            except queue.Empty:
                if deadline is not None and time.monotonic()>deadline:raise TimeoutError('Peer world startup timed out') from None
                if any(not p.is_alive() for p in self.local_processes) or self.peer.failed:
                    self.failed=True;raise RuntimeError('A local or peer worker exited') from None
    def close(self):
        if self._closed:return
        self._closed=True;self.ready=False;self.cancel.set();self.events.put(('shutdown',None,None));self.peer.close()
        for jobs in self.jobs[:8]:jobs.put(None)
        deadline=time.monotonic()+10
        for p in self.local_processes:
            if p.pid is not None:p.join(max(0,deadline-time.monotonic()))
        for p in self.local_processes:
            if p.is_alive():p.terminate()
        for p in self.local_processes:
            if p.pid is not None:
                p.join(5)
                if p.is_alive():p.kill();p.join()
        self.peer.finish(max(0,deadline-time.monotonic())+5)
