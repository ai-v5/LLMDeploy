"""Peer agent spawns only its eight owned ranks; EOF closes the same world."""
from dataclasses import asdict
import json
import multiprocessing as mp
import queue
import socket
import subprocess
import sys
import threading
import time
from recipe_api.backend import EngineConfig,worker
from wire import Channel

def run_agent(address,run_id,worker_python,*,worker_target=worker,connect_timeout=1800):
    deadline=time.monotonic()+connect_timeout
    while True:
        try:sock=socket.create_connection(address,timeout=5);break
        except OSError:
            if time.monotonic()>deadline:raise
            time.sleep(1)
    channel=Channel(sock);sock.settimeout(30)
    channel.send({'kind':'hello','version':1,'run_id':run_id,'ranks':list(range(8,16))})
    hello=channel.receive()
    if hello.get('kind')!='config' or hello.get('run_id')!=run_id:raise ValueError('Invalid coordinator config')
    config=EngineConfig(**hello['config']);assert (config.dp,config.ep)==(16,16)
    sock.settimeout(None);mp.set_executable(worker_python);context=mp.get_context('spawn')
    paths=json.loads(subprocess.check_output([worker_python,'-c','import json,sys;print(json.dumps(sys.path))'],text=True))
    events=context.Queue();cancel=context.Event();jobs=[context.Queue() for _ in range(8)]
    processes=[context.Process(target=worker_target,args=(rank,config,jobs[rank-8],events,cancel,paths)) for rank in range(8,16)]
    stopping=threading.Event();owners={};failure=[]
    def read():
        try:
            while True:
                message=channel.receive()
                if message['kind']=='stop':stopping.set();return
                if message['kind']!='job' or message['rank'] not in range(8,16):raise ValueError('Invalid coordinator job')
                rank,value=message['rank'],message['message']
                if value is not None and value[0]=='add':owners[value[1]]=rank
                jobs[rank-8].put(value)
        except BaseException as error:failure.append(error);stopping.set()
    try:
        for rank,p in enumerate(processes,8):
            p.start();channel.send({'kind':'started','rank':rank})
        threading.Thread(target=read,daemon=True).start()
        while not stopping.is_set():
            try:
                event=events.get(timeout=.2);kind,identifier,detail=event
                rank=identifier if kind in ('ready','cache','idle','error') else owners.get(identifier)
                if rank not in range(8,16):raise ValueError('Unowned native event')
                channel.send({'kind':'event','rank':rank,'event':event})
                if kind in ('finished','cancelled'):owners.pop(identifier,None)
            except queue.Empty:
                for rank,p in enumerate(processes,8):
                    if not p.is_alive():channel.send({'kind':'exit','rank':rank,'returncode':p.exitcode});raise RuntimeError('Owned remote rank exited')
        if failure:raise RuntimeError('Coordinator disconnected') from failure[0]
    finally:
        cancel.set()
        for q in jobs:q.put(None)
        deadline=time.monotonic()+10
        for p in processes:
            if p.pid is not None:p.join(max(0,deadline-time.monotonic()))
        for p in processes:
            if p.is_alive():p.terminate()
        for p in processes:
            if p.pid is not None:
                p.join(5)
                if p.is_alive():p.kill();p.join()
        try:channel.send({'kind':'stopped'})
        except OSError:pass
        channel.close()
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--coordinator',required=True);p.add_argument('--port',required=True,type=int);p.add_argument('--run-id',required=True)
    a=p.parse_args();run_agent((a.coordinator,a.port),a.run_id,sys.executable)
