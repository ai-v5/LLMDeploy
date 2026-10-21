from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
import unittest

from deploy_gateway.routing import Busy, SessionRouter, Unavailable
from deploy_gateway.server import Gateway


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def router(self, **kwargs):
        router = SessionRouter([21001, 21002, 21003], self.root/'bindings.json', **kwargs)
        router.healthy = dict.fromkeys(router.ports, True)
        return router

    def test_contended_three_backend_hard_capacity(self):
        router = self.router(max_active=2, max_waiting=0)
        start = threading.Event()
        def request(index):
            start.wait()
            try:
                return router.acquire(hashlib.sha256(str(index).encode()).hexdigest())[0]
            except Busy:
                return None
        with ThreadPoolExecutor(30) as pool:
            futures = [pool.submit(request, i) for i in range(30)]
            start.set()
            acquired = [future.result() for future in futures]
        self.assertEqual(sum(p is not None for p in acquired), 6)
        self.assertEqual(list(router.inflight.values()), [2, 2, 2])
        for port in acquired:
            if port is not None:
                router.end(port)
        self.assertEqual(sum(router.inflight.values()), 0)

    def test_affinity_spills_when_owner_is_full(self):
        router = self.router(max_active=1, max_waiting=0)
        first, _ = router.acquire('a'*64)
        other, _ = router.acquire('a'*64)
        self.assertNotEqual(first, other)
        self.assertEqual(router.bindings['a'*64], other)
        router.end(first); router.end(other)
        self.assertEqual(router.acquire('a'*64)[0], other)
        router.end(other)

    def test_new_session_prefers_less_busy_node_over_fewer_bindings(self):
        router = self.router(max_active=2)
        router.enabled_ports = set(router.ports[:2])
        router.bindings = {str(i)*64:router.ports[1] for i in range(8)}
        busy,_ = router.acquire('a'*64)
        self.assertEqual(busy,router.ports[0])
        less_busy,_ = router.acquire('b'*64)
        self.assertEqual(less_busy,router.ports[1])
        router.end(busy);router.end(less_busy)

    def test_bounded_wait_queue_releases_on_capacity(self):
        router = self.router(max_active=1, max_waiting=1, queue_timeout=1)
        held = [router.acquire(str(i)*64)[0] for i in range(3)]
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(router.acquire, 'f'*64)
            deadline = time.monotonic()+1
            while router.waiting != 1 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(router.waiting, 1)
            with self.assertRaisesRegex(Busy, 'QUEUE_FULL'):
                router.acquire('e'*64)
            router.end(held.pop())
            port, _ = pending.result(timeout=1)
            self.assertEqual(router.waiting, 0)
            router.end(port)
        for port in held:
            router.end(port)

    def test_timeout_does_not_leak_waiting_count(self):
        router = self.router(max_active=1, max_waiting=1, queue_timeout=.03)
        held = [router.acquire(str(i)*64)[0] for i in range(3)]
        with self.assertRaisesRegex(Busy, 'QUEUE_TIMEOUT'):
            router.acquire('d'*64)
        self.assertEqual(router.waiting, 0)
        for port in held:
            router.end(port)

    def test_drain_preserves_active_lease_but_rejects_new_ones(self):
        router = self.router(max_active=2)
        first, _ = router.acquire('a'*64)
        with router.lock:
            router.enabled_ports.remove(first)
        second, _ = router.acquire('a'*64)
        self.assertNotEqual(first, second)
        self.assertEqual(router.inflight[first], 1)
        router.end(first); router.end(second)
        router.enabled_ports.clear()
        with self.assertRaises(Unavailable):
            router.acquire('b'*64)

    def test_invalid_capacity_configuration_rejected(self):
        for options in [{'max_active':0}, {'max_waiting':-1}, {'queue_timeout':0}, {'queue_timeout':float('nan')}]:
            with self.assertRaises(ValueError):
                self.router(**options)


class CancellationModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200); self.send_header('Content-Length','0'); self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        self.server.rank = self.headers.get('X-data-parallel-rank')
        self.server.started.set()
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.connection], [], [], .02)
            if ready:
                try:
                    closed = not self.connection.recv(1, socket.MSG_PEEK)
                except OSError:
                    closed = True
                if closed:
                    self.server.cancelled.set()
                    return


class DisconnectTests(unittest.TestCase):
    def test_disconnect_during_prefill_aborts_without_marking_node_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root/'keys.json'
            # Synthetic fixture, never a live credential.
            token = 'synthetic-admission-test'
            store.write_text(json.dumps({'version':1,'records':{'test':{'sha256':hashlib.sha256(token.encode()).hexdigest(),'active':True}}}))
            model = ThreadingHTTPServer(('127.0.0.1',0), CancellationModel)
            model.started, model.cancelled = threading.Event(), threading.Event()
            model.daemon_threads = True
            thread = threading.Thread(target=model.serve_forever,kwargs={'poll_interval':.02},daemon=True);thread.start()
            gateway = Gateway(('127.0.0.1',0),store,bindings_file=root/'bindings.json',backend_ports=[model.server_port],backend_managed_affinity=True)
            gateway.backends.stop.set();gateway.backends.thread.join(2)
            worker = threading.Thread(target=gateway.serve_forever,kwargs={'poll_interval':.02},daemon=True);worker.start()
            try:
                client = HTTPConnection('127.0.0.1',gateway.server_port,timeout=2)
                body = json.dumps({'messages':[{'role':'user','content':'test prefill cancellation'}]})
                client.request('POST','/v1/chat/completions',body,{'Content-Type':'application/json','Authorization':'Bearer '+token})
                self.assertTrue(model.started.wait(1))
                client.close()
                self.assertTrue(model.cancelled.wait(1), 'prefill connection was not cancelled promptly')
                deadline = time.monotonic()+1
                while sum(gateway.backends.inflight.values()) and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertEqual(sum(gateway.backends.inflight.values()),0)
                self.assertTrue(gateway.backends.healthy[model.server_port])
                self.assertIsNone(model.rank, 'native DP routing must not be overridden')
            finally:
                gateway.shutdown();gateway.server_close();worker.join(2)
                model.shutdown();model.server_close();thread.join(2)


class AdmissionModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200);self.send_header('Content-Length','0');self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.observed.append(dict(self.headers))
        if body.get('hold'):
            self.server.started.set()
            self.server.release.wait(3)
        response = json.dumps({'port':self.server.server_port}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(response)));self.end_headers()
        self.wfile.write(response)


class PoolHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.models = []
        for _ in range(3):
            model = ThreadingHTTPServer(('127.0.0.1',0),AdmissionModel)
            model.daemon_threads = True
            model.started,model.release = threading.Event(),threading.Event()
            model.observed = []
            threading.Thread(target=model.serve_forever,kwargs={'poll_interval':.02},daemon=True).start()
            self.models.append(model)
        self.ports = [m.server_port for m in self.models]
        self.control = self.root/'control.json'
        self.control.write_text(json.dumps({'enabled_ports':self.ports}))
        self.token = 'synthetic-three-node-fixture'
        store = self.root/'keys.json'
        store.write_text(json.dumps({'version':1,'records':{'test':{'sha256':hashlib.sha256(self.token.encode()).hexdigest(),'active':True}}}))
        self.gateway = Gateway(('127.0.0.1',0),store,bindings_file=self.root/'bindings.json',
                               backend_ports=self.ports,backend_managed_affinity=True,
                               max_active=1,max_waiting=0,control_file=self.control)
        self.gateway.backends.stop.set();self.gateway.backends.thread.join(2)
        threading.Thread(target=self.gateway.serve_forever,kwargs={'poll_interval':.02},daemon=True).start()
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for model in self.models:model.release.set()
        self.gateway.shutdown();self.gateway.server_close()
        for model in self.models:model.shutdown();model.server_close()

    def request(self, session, **body):
        client = HTTPConnection('127.0.0.1',self.gateway.server_port,timeout=4)
        try:
            client.request('POST','/v1/chat/completions',json.dumps(body),
                           {'Authorization':'Bearer '+self.token,'x-session-id':session,
                            'x-deepinfer-cache-namespace':'synthetic-cache','Content-Type':'application/json'})
            response = client.getresponse()
            return response.status,dict(response.getheaders()),json.loads(response.read())
        finally:client.close()

    def test_three_node_http_routing_drain_and_fail_closed_control(self):
        used = {self.request(str(i))[2]['port'] for i in range(3)}
        self.assertEqual(used,set(self.ports))
        for model in self.models:
            headers = {k.lower():v for k,v in model.observed[0].items()}
            self.assertNotIn('authorization',headers)
            self.assertNotIn('x-data-parallel-rank',headers)
            self.assertEqual(headers['x-deepinfer-cache-namespace'],'synthetic-cache')
        self.control.write_text(json.dumps({'enabled_ports':[self.ports[-1]]}))
        self.gateway.backends.probe()
        self.assertEqual({self.request(str(i))[2]['port'] for i in range(3)},{self.ports[-1]})
        self.control.write_text('{invalid')
        self.gateway.backends.probe()
        self.assertEqual(self.request('closed')[0],503)
        self.control.write_text(json.dumps({'enabled_ports':self.ports}))
        self.gateway.backends.probe()
        self.assertEqual(self.request('recovered')[0],200)

    def test_full_pool_returns_429_retry_after_without_oversubscription(self):
        with ThreadPoolExecutor(3) as pool:
            pending = [pool.submit(self.request,'held-'+str(i),hold=True) for i in range(3)]
            for model in self.models:self.assertTrue(model.started.wait(1))
            status,headers,_ = self.request('overflow')
            self.assertEqual(status,429)
            self.assertEqual(headers['Retry-After'],'1')
            self.assertEqual(list(self.gateway.backends.inflight.values()),[1,1,1])
            self.assertTrue(all(self.gateway.backends.healthy.values()))
            for model in self.models:model.release.set()
            self.assertTrue(all(f.result()[0]==200 for f in pending))
        self.assertEqual(sum(self.gateway.backends.inflight.values()),0)
