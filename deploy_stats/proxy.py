"""A loopback-only, byte-transparent accounting hop before the existing gateway."""
import asyncio
from contextlib import suppress
from email.parser import Parser
from http.client import HTTPMessage
import ipaddress
import os
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

from .capture import SafeCapture
from .session import MAX_BODY, SessionBodyCapture, explicit_session, key_digest
from .store import Recorder

CLIENT_HEADER = 'x-llmdeploy-client-ip'
PROTOCOLS = {'/v1/chat/completions': 'chat', '/v1/responses': 'responses',
             '/v1/messages': 'messages'}
# Allow a large incoming header plus additional fields from the front proxy.
MAX_HEADER = 1024 * 1024 + 65536
MAX_RESPONSE_HEADER = 10 * 1024 * 1024
WINDOWS_WORKERS = 3
WINDOWS_CONNECTIONS_PER_WORKER = 160
MAX_CONNECTIONS = WINDOWS_WORKERS * WINDOWS_CONNECTIONS_PER_WORKER


def client_ip(headers):
    values = headers.get_all(CLIENT_HEADER, [])
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    try:
        address = ipaddress.ip_address(values[0].strip())
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return None


def session_fingerprint(headers):
    return explicit_session(headers)[1]


def _headers(raw):
    # Match http.client.parse_headers, used by the existing gateway: header
    # octets are decoded as ISO-8859-1 before the email parser sees them.
    return Parser(_class=HTTPMessage).parsestr((raw + b'\r\n').decode('iso-8859-1'))


async def _read(reader, size, timeout):
    return await asyncio.wait_for(reader.read(size), timeout)


async def _readexactly(reader, size, timeout):
    return await asyncio.wait_for(reader.readexactly(size), timeout)


async def _line(reader, timeout):
    return await asyncio.wait_for(reader.readuntil(b'\r\n'), timeout)


async def _response_body(reader, writer, headers, method, status, capture, timeout):
    async def send(raw, observe=False):
        if observe and capture:
            capture.feed(raw)
        writer.write(raw)
        await writer.drain()

    if method == 'HEAD' or status in (204, 304) or 100 <= status < 200:
        return
    encoding = (headers.get('Transfer-Encoding') or '').lower()
    lengths = headers.get_all('Content-Length', [])
    if encoding:
        if encoding.strip() != 'chunked' or lengths:
            raise ValueError('INVALID_RESPONSE_FRAMING')
        while True:
            line = await _line(reader, timeout)
            size = int(line[:-2].split(b';', 1)[0], 16)
            if size < 0:
                raise ValueError('INVALID_CHUNK_SIZE')
            await send(line)
            if size == 0:
                while True:
                    trailer = await _line(reader, timeout)
                    await send(trailer)
                    if trailer == b'\r\n':
                        return
            left = size
            while left:
                data = await _read(reader, min(left, 65536), timeout)
                if not data:
                    raise asyncio.IncompleteReadError(b'', left)
                await send(data, True)
                left -= len(data)
            suffix = await _readexactly(reader, 2, timeout)
            if suffix != b'\r\n':
                raise ValueError('INVALID_CHUNK_END')
            await send(suffix)
    elif lengths:
        if len(lengths) != 1:
            raise ValueError('INVALID_RESPONSE_LENGTH')
        left = int(lengths[0])
        if left < 0:
            raise ValueError('INVALID_RESPONSE_LENGTH')
        while left:
            data = await _read(reader, min(left, 65536), timeout)
            if not data:
                raise asyncio.IncompleteReadError(b'', left)
            await send(data, True)
            left -= len(data)
    else:
        while True:
            data = await _read(reader, 65536, timeout)
            if not data:
                return
            await send(data, True)


class Proxy:
    """Async transfer keeps cancellation independent from slow upstream reads."""

    def __init__(self, port, upstream_port, database, *, timeout=10800):
        if type(port) is not int or type(upstream_port) is not int or not 0 <= port <= 65535 or not 1 <= upstream_port <= 65535:
            raise ValueError('INVALID_PORT')
        if not 0 < timeout <= 86400:
            raise ValueError('INVALID_TIMEOUT')
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.socket.bind(('127.0.0.1', port))
            self.socket.listen(MAX_CONNECTIONS)
            self.socket.setblocking(False)
            self.server_port = self.socket.getsockname()[1]
            if self.server_port == upstream_port:
                raise ValueError('UPSTREAM_EQUALS_PROXY')
            self.recorder = Recorder(database)
        except Exception:
            self.socket.close()
            raise
        self.upstream_port = upstream_port
        self.timeout = timeout
        self.active_lock = threading.Lock()
        self.active = 0
        self.loop = None
        self.stop = None
        self.finished = threading.Event()
        self.ready = threading.Event()
        self.tasks = set()
        self.tasks_lock = threading.Lock()
        self.connections_lock = threading.Lock()
        self.connections = 0
        self.shutdown_requested = threading.Event()

    def serve_forever(self, poll_interval=None):
        try:
            if os.name == 'nt':
                self._serve_windows()
            else:
                asyncio.run(self._serve())
        finally:
            self.finished.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        server = await asyncio.start_server(self._handle, sock=self.socket, limit=MAX_HEADER)
        self.ready.set()
        async with server:
            await self.stop.wait()
        with self.tasks_lock:
            tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def shutdown(self):
        if self.ready.wait(3):
            self.shutdown_requested.set()
            if self.loop and self.stop:
                self.loop.call_soon_threadsafe(self.stop.set)
            self.finished.wait(5)

    def _serve_windows(self):
        # One Windows selector can watch only 512 sockets. Dispatch accepted
        # connections across independent loops, each with room for both the
        # client and upstream sockets, instead of using the reset-prone proactor.
        workers = []
        worker_lock = threading.Lock()
        started = [threading.Event() for _ in range(WINDOWS_WORKERS)]
        loops = [None] * WINDOWS_WORKERS
        stops = [None] * WINDOWS_WORKERS
        assigned = [0] * WINDOWS_WORKERS
        errors = []

        async def worker(index):
            loop = asyncio.get_running_loop()
            loops[index], stops[index] = loop, asyncio.Event()
            started[index].set()
            await stops[index].wait()
            with self.tasks_lock:
                tasks = [task for task in self.tasks if task.get_loop() is loop]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        def run_worker(index):
            try:
                with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                    runner.run(worker(index))
            except Exception as exc:
                errors.append(exc)
                started[index].set()
                self.shutdown_requested.set()

        async def handle_socket(conn, index):
            try:
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader(limit=MAX_HEADER)
                protocol = asyncio.StreamReaderProtocol(reader)
                transport, _ = await loop.connect_accepted_socket(lambda: protocol, conn)
                writer = asyncio.StreamWriter(transport, protocol, reader, loop)
                await self._handle(reader, writer)
            finally:
                conn.close()
                with worker_lock:
                    assigned[index] -= 1

        def dispatch(conn, index):
            task = asyncio.create_task(handle_socket(conn, index))
            # Track handoff as well as the handler so shutdown cannot leave an
            # accepted but not-yet-attached socket behind.
            with self.tasks_lock:
                self.tasks.add(task)
            task.add_done_callback(lambda done: self._discard_task(done))

        try:
            for index in range(WINDOWS_WORKERS):
                thread = threading.Thread(target=run_worker, args=(index,), daemon=True)
                workers.append(thread)
                thread.start()
            if not all(event.wait(3) for event in started) or errors:
                raise RuntimeError('PROXY_WORKER_START_FAILED')
            self.socket.settimeout(.2)
            self.ready.set()
            while not self.shutdown_requested.is_set():
                try:
                    conn, _ = self.socket.accept()
                except socket.timeout:
                    continue
                conn.setblocking(False)
                with worker_lock:
                    index = min(range(WINDOWS_WORKERS), key=assigned.__getitem__)
                    if assigned[index] < WINDOWS_CONNECTIONS_PER_WORKER:
                        assigned[index] += 1
                    else:
                        index = None
                if index is None:
                    self.recorder.note_drop()
                    with suppress(OSError):
                        # Give an already-arriving head a short chance to drain;
                        # closing with unread bytes otherwise resets Windows peers.
                        conn.settimeout(.05)
                        conn.recv(MAX_HEADER)
                    with suppress(OSError):
                        conn.sendall(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    conn.close()
                else:
                    loops[index].call_soon_threadsafe(dispatch, conn, index)
        finally:
            for loop, stop in zip(loops, stops):
                if loop and stop:
                    with suppress(RuntimeError):
                        loop.call_soon_threadsafe(stop.set)
            for thread in workers:
                thread.join(5)

    def _discard_task(self, task):
        with self.tasks_lock:
            self.tasks.discard(task)

    def server_close(self):
        if self.ready.is_set() and not self.finished.is_set():
            self.shutdown()
            if not self.finished.is_set():
                raise RuntimeError('PROXY_SHUTDOWN_TIMEOUT')
        self.socket.close()
        with self.active_lock:
            with self.recorder.lock:
                self.recorder.dropped += self.active
            self.recorder.close()

    async def _handle(self, client_reader, client_writer):
        task = asyncio.current_task()
        with self.tasks_lock:
            self.tasks.add(task)
        with self.connections_lock:
            accepted = self.connections < MAX_CONNECTIONS
            if accepted:
                self.connections += 1
        if not accepted:
            self.recorder.note_drop()
            # Read the request head before closing so Windows does not reset a
            # small request while the caller is trying to receive the 429.
            with suppress(OSError, ConnectionError, asyncio.TimeoutError, asyncio.LimitOverrunError,
                          asyncio.IncompleteReadError):
                await asyncio.wait_for(client_reader.readuntil(b'\r\n\r\n'), 1)
            with suppress(OSError, ConnectionError):
                client_writer.write(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await client_writer.drain()
            client_writer.close()
            with suppress(OSError, ConnectionError, asyncio.TimeoutError):
                await asyncio.wait_for(client_writer.wait_closed(), 1)
            self._discard_task(task)
            return
        started = time.perf_counter()
        started_wall = time.time()
        counted = False
        source = None
        session = None
        has_session_header = False
        request_id = None
        first_visible = None
        status, state, values = None, 'interrupted', None
        sent_headers = False
        upstream_writer = None
        body_task = watch_task = copy_task = head_task = None
        body_done = asyncio.Event()
        abort_send = asyncio.Event()
        disconnected = asyncio.Event()
        try:
            raw = await asyncio.wait_for(client_reader.readuntil(b'\r\n\r\n'), 30)
            request_line, _, header_bytes = raw[:-4].partition(b'\r\n')
            method_bytes, target_bytes, version = request_line.split(b' ', 2)
            method = method_bytes.decode('ascii')
            target = target_bytes.decode('ascii')
            if version not in (b'HTTP/1.0', b'HTTP/1.1'):
                raise ValueError('INVALID_HTTP_VERSION')
            path = urlsplit(target).path
            counted = method == 'POST' and path.startswith('/v1/')
            protocol = PROTOCOLS.get(path)
            headers = _headers(header_bytes)
            source = client_ip(headers)
            if counted:
                has_session_header, session = explicit_session(headers)
                request_id = uuid.uuid4().hex
                with self.active_lock:
                    self.active += 1
                try:
                    self.recorder.begin(request_id, started_wall, source, session)
                except Exception:
                    self.recorder.note_drop()
            lines = header_bytes.split(b'\r\n')
            if any(line.startswith((b' ', b'\t')) for line in lines):
                raise ValueError('FOLDED_HEADER_UNSUPPORTED')
            forward = [line for line in lines if line.split(b':', 1)[0].strip().lower()
                       not in (CLIENT_HEADER.encode(), b'connection')]
            forward.append(b'Connection: close')
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', self.upstream_port, limit=MAX_RESPONSE_HEADER), 5)
            upstream_writer.write(request_line + b'\r\n' + b'\r\n'.join(forward) + b'\r\n\r\n')
            await upstream_writer.drain()
            lengths = headers.get_all('Content-Length', [])
            try:
                length = int(lengths[0]) if len(lengths) == 1 else 0
            except ValueError:
                length = 0
            if headers.get('Transfer-Encoding') or len(lengths) > 1 or not 0 <= length <= 64 * 1024 * 1024:
                length = 0  # Existing gateway rejects the unchanged headers.
            body_capture = None
            if counted and not has_session_header and 0 < length <= MAX_BODY:
                try:
                    body_capture = SessionBodyCapture(key_digest(headers))
                except Exception:
                    self.recorder.note_drop()

            async def send_body():
                nonlocal session, body_capture
                left = length
                try:
                    while left:
                        chunk = await _read(client_reader, min(left, 65536), 30)
                        if not chunk:
                            disconnected.set()
                            upstream_writer.close()
                            return
                        left -= len(chunk)
                        if not abort_send.is_set():
                            if body_capture:
                                try:
                                    body_capture.feed(chunk)
                                except Exception:
                                    with suppress(Exception):
                                        body_capture.close()
                                    body_capture = None
                                    self.recorder.note_drop()
                            try:
                                upstream_writer.write(chunk)
                                await upstream_writer.drain()
                                await asyncio.sleep(0)
                            except (OSError, ConnectionError):
                                abort_send.set()
                        elif body_capture:
                            with suppress(Exception):
                                body_capture.close()
                            body_capture = None
                except Exception:
                    disconnected.set()
                    upstream_writer.close()
                finally:
                    try:
                        if body_capture:
                            if left == 0 and not disconnected.is_set():
                                session = body_capture.finish()
                                if session is not None:
                                    self.recorder.set_session(request_id, session)
                            else:
                                body_capture.close()
                    except Exception:
                        if body_capture:
                            with suppress(Exception):
                                body_capture.close()
                        self.recorder.note_drop()
                    finally:
                        body_done.set()

            async def watch_client():
                await body_done.wait()
                try:
                    while await client_reader.read(65536):
                        pass
                except Exception:
                    pass
                disconnected.set()
                upstream_writer.close()

            head_task = asyncio.create_task(asyncio.wait_for(
                upstream_reader.readuntil(b'\r\n\r\n'), self.timeout))
            if length:
                # Let an immediate auth/admission rejection arrive before a
                # large upload can reset an upstream that does not read it.
                deadline = asyncio.get_running_loop().time() + (.1 if length >= 4096 else .01)
                while True:
                    remaining = max(0, deadline - asyncio.get_running_loop().time())
                    done, _ = await asyncio.wait((head_task,), timeout=remaining)
                    if not done:
                        break
                    preliminary = head_task.result()
                    parts = preliminary.split(b'\r\n', 1)[0].split(b' ', 2)
                    if len(parts) < 2 or not parts[1].isdigit():
                        break
                    code = int(parts[1])
                    if code < 100 or code > 599:
                        raise ValueError('INVALID_RESPONSE_STATUS')
                    if code >= 200:
                        abort_send.set()
                        break
                    client_writer.write(preliminary)
                    await client_writer.drain()
                    head_task = asyncio.create_task(asyncio.wait_for(
                        upstream_reader.readuntil(b'\r\n\r\n'), self.timeout))
            body_task = asyncio.create_task(send_body())
            watch_task = asyncio.create_task(watch_client())
            while True:
                response_head = await head_task
                status_line, _, response_fields = response_head[:-4].partition(b'\r\n')
                status_parts = status_line.split(b' ', 2)
                status = int(status_parts[1])
                if status < 100 or status > 599:
                    raise ValueError('INVALID_RESPONSE_STATUS')
                if status >= 200:
                    fields = [line for line in response_fields.split(b'\r\n') if line and
                              line.split(b':', 1)[0].strip().lower() != b'connection']
                    response_head = status_line + b'\r\n' + b'\r\n'.join(fields + [b'Connection: close']) + b'\r\n\r\n'
                client_writer.write(response_head)
                await client_writer.drain()
                if status >= 200:
                    sent_headers = True
                    break
                head_task = asyncio.create_task(asyncio.wait_for(
                    upstream_reader.readuntil(b'\r\n\r\n'), self.timeout))
            if not body_done.is_set():
                abort_send.set()
            response_headers = _headers(response_fields)
            def first_output():
                nonlocal first_visible
                if first_visible is None:
                    first_visible = max(0, time.perf_counter() - started)
                    if request_id is not None:
                        self.recorder.first_output(request_id, first_visible)

            capture = SafeCapture(protocol, response_headers.get('Content-Type'),
                                   response_headers.get('Content-Encoding'), first_output) if protocol else None
            copy_task = asyncio.create_task(_response_body(upstream_reader, client_writer, response_headers,
                method, status, capture, self.timeout))
            done, _ = await asyncio.wait((copy_task, watch_task), return_when=asyncio.FIRST_COMPLETED)
            error_response_completed = (status >= 400 and copy_task in done and
                                        not copy_task.cancelled() and copy_task.exception() is None)
            if disconnected.is_set() and not error_response_completed:
                copy_task.cancel()
                await asyncio.gather(copy_task, return_exceptions=True)
            else:
                await copy_task
                if status >= 400:
                    state = 'failed'
                elif capture and (values := capture.finish()) is not None:
                    state = 'known'
                else:
                    state = 'missing'
        except Exception:
            if not sent_headers and not disconnected.is_set():
                with suppress(OSError, ConnectionError):
                    status = 502
                    client_writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    await client_writer.drain()
        finally:
            abort_send.set()
            if body_task and not body_task.done():
                with suppress(asyncio.TimeoutError, OSError, ConnectionError):
                    await asyncio.wait_for(asyncio.shield(body_task), 2)
            pending_tasks = [pending for pending in (body_task, watch_task, copy_task, head_task) if pending]
            for pending in pending_tasks:
                if pending and not pending.done():
                    pending.cancel()
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            if upstream_writer:
                upstream_writer.close()
                with suppress(OSError, ConnectionError, asyncio.TimeoutError):
                    await asyncio.wait_for(upstream_writer.wait_closed(), 1)
            client_writer.close()
            with suppress(OSError, ConnectionError, asyncio.TimeoutError):
                await asyncio.wait_for(client_writer.wait_closed(), 1)
            if counted:
                prompt, output, cached = values if state == 'known' else (None, None, None)
                with self.active_lock:
                    try:
                        self.recorder.end(request_id)
                        self.recorder.submit((time.time(), source, max(0, time.perf_counter() - started),
                            status, state, prompt, output, cached, session, first_visible), request_id)
                    except Exception:
                        self.recorder.note_drop()
                    finally:
                        self.active -= 1
            with self.connections_lock:
                self.connections -= 1
            self._discard_task(task)
