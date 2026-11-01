"""Observe gateway responses behind Caddy's verified default forwarding policy."""
import time
import threading
import uuid
from urllib.parse import urlsplit

from .capture import SafeCapture
from .proxy import PROTOCOLS, client_ip
from .session import BodyBudget
from .store import Recorder


CAPTURE_BUDGET = 32 * 1024 * 1024
MAX_CAPTURE_FRAME = 4 * 1024 * 1024
MAX_CAPTURE_STRUCTURE = 16384
MAX_CAPTURE_DELTA = 64 * 1024


class _Observer:
    def __init__(self, recorder, budget, headers, protocol):
        self.recorder = recorder
        self.budget = budget
        self.protocol = protocol
        self.started = time.perf_counter()
        self.request_id = uuid.uuid4().hex
        # Caddy must be verified to overwrite untrusted X-Forwarded-For.
        # Direct loopback callers do not have that provenance guarantee.
        self.source = client_ip(headers, header='X-Forwarded-For')
        self.session_id = None
        self.first = None
        self.status = None
        self.completed = False
        self.capture = None
        self.reserved = 0
        self.failed = False
        self.recorder.begin(self.request_id, time.time(), self.source, None)

    def fail(self):
        if not self.failed:
            self.failed = True
            if self.reserved:
                self.budget.release(self.reserved)
                self.reserved = 0
            self.capture = None
            try:
                self.recorder.note_drop()
            except Exception:
                pass

    def session(self, value):
        if not self.failed and value is not None:
            self.session_id = value
            self.recorder.set_session(self.request_id, value)

    def error(self, status):
        self.status = status

    def response(self, status, content_type, content_encoding):
        self.status = status
        if not self.failed and status < 400 and self.protocol:
            def first_output():
                if self.first is None:
                    self.first = max(0, time.perf_counter() - self.started)
                    self.recorder.first_output(self.request_id, self.first)
            self.capture = SafeCapture(self.protocol, content_type, content_encoding, first_output,
                                       max_frame=MAX_CAPTURE_FRAME, max_structure=MAX_CAPTURE_STRUCTURE,
                                       max_delta=MAX_CAPTURE_DELTA)

    def chunk(self, data):
        if self.failed or self.capture is None:
            return
        if not self.budget.reserve(len(data)):
            self.fail()
            return
        self.reserved += len(data)
        self.capture.feed(data)
        if self.capture.failed:
            self.fail()
            return
        retained = len(self.capture.reader.buffer) + self.capture.reader.event_bytes
        released = self.reserved - retained
        if released > 0:
            self.budget.release(released)
            self.reserved -= released

    def complete(self, clean):
        self.completed = clean

    def finish(self):
        state, values = 'interrupted', None
        try:
            if self.status is not None and self.status >= 400:
                state = 'failed'
            elif self.completed:
                if not self.failed and self.capture is not None:
                    values = self.capture.finish()
                state = 'known' if values is not None else 'missing'
        except Exception:
            self.fail()
            state, values = 'missing', None
        finally:
            if self.reserved:
                self.budget.release(self.reserved)
                self.reserved = 0
            self.capture = None
        prompt, output, cached = values if state == 'known' else (None, None, None)
        try:
            try:
                self.recorder.submit((time.time(), self.source, max(0, time.perf_counter() - self.started),
                                      self.status, state, prompt, output, cached, self.session_id, self.first),
                                     self.request_id)
            finally:
                self.recorder.end(self.request_id)
        except Exception:
            try:
                self.recorder.note_drop()
            except Exception:
                pass


def make_handler(base_handler, recorder, *, budget=None):
    """Wrap one gateway handler; recorder failure never replaces its response."""
    budget = budget or BodyBudget(CAPTURE_BUDGET)

    class InstrumentedHandler(base_handler):
        _llm_stats_wrapped = True

        def proxy(self):
            try:
                path = urlsplit(self.path).path
            except ValueError:
                return super().proxy()
            if self.command != 'POST' or not path.startswith('/v1/'):
                return super().proxy()
            observer = None
            try:
                observer = _Observer(recorder, budget, self.headers, PROTOCOLS.get(path))
            except Exception:
                try:
                    recorder.note_drop()
                except Exception:
                    pass
            self._stats_observer = observer
            try:
                return super().proxy()
            finally:
                if observer is not None:
                    try:
                        observer.finish()
                    except Exception:
                        observer.fail()
                self._stats_observer = None

        def error(self, status, message):
            self._observe('error', status)
            return super().error(status, message)

    return InstrumentedHandler


def install(module, database):
    """Enable capture on this gateway module; unavailable storage fails open."""
    if (getattr(module.Handler, '_llm_stats_wrapped', False) or
            not callable(getattr(module.Handler, '_observe', None))):
        return None
    try:
        recorder = Recorder(database)
    except Exception:
        return None
    try:
        module.Handler = make_handler(module.Handler, recorder)
    except Exception:
        recorder.close()
        return None
    return recorder


def serve(module, database, make_servers, *args, **kwargs):
    """Run an existing two-server bridge with fail-open, in-process capture."""
    original_handler = module.Handler
    recorder = install(module, database)
    installed_handler = module.Handler if recorder is not None else None
    inner = outer = None
    inner_started = False
    try:
        inner, outer = make_servers(*args, **kwargs)
        worker = threading.Thread(target=inner.serve_forever, daemon=True)
        worker.start()
        inner_started = True
        outer.serve_forever()
    finally:
        try:
            if outer is not None:
                outer.server_close()
        finally:
            try:
                if inner is not None:
                    try:
                        if inner_started:
                            inner.shutdown()
                    finally:
                        inner.server_close()
            finally:
                if recorder is not None:
                    if module.Handler is installed_handler:
                        module.Handler = original_handler
                    recorder.close()
