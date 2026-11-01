"""Bounded, body-blind draining after an early gateway rejection."""
from contextlib import suppress
import queue
import select
import socket
import threading
import time


# Keep a full forwarding wave without exceeding Windows select()'s 512-fd set.
MAX_REJECTIONS = 480
MAX_DRAIN_BYTES = 128 * 1024 * 1024
DRAIN_TIMEOUT = 30


class RejectionDrainer:
    """Keep rejected sockets readable without holding a request admission slot."""

    def __init__(self):
        self._slots = threading.BoundedSemaphore(MAX_REJECTIONS)
        self._pending = queue.SimpleQueue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.full = 0
        self.duplicate_failures = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, connection):
        with self._lock:
            if self._stop.is_set():
                return False
            if not self._slots.acquire(blocking=False):
                self.full += 1
                return False
            duplicate = None
            try:
                duplicate = connection.dup()
                duplicate.setblocking(False)
                self._pending.put(duplicate)
                return True
            except OSError:
                self.duplicate_failures += 1
                if duplicate is not None:
                    duplicate.close()
                self._slots.release()
                return False

    def close(self):
        with self._lock:
            self._stop.set()
        self.thread.join(1)

    def _run(self):
        active = {}

        def release(conn):
            active.pop(conn, None)
            conn.close()
            self._slots.release()

        while not self._stop.is_set():
            while True:
                try:
                    conn = self._pending.get_nowait()
                except queue.Empty:
                    break
                active[conn] = [time.monotonic() + DRAIN_TIMEOUT, MAX_DRAIN_BYTES]
            if not active:
                self._stop.wait(.02)
                continue
            try:
                readable, _, failed = select.select(list(active), [], list(active), .02)
            except (OSError, ValueError):
                readable, failed = [], list(active)
            now = time.monotonic()
            for conn in set(readable + failed + [conn for conn, (deadline, _) in active.items()
                                                if deadline <= now]):
                if conn not in active:
                    continue
                deadline, remaining = active[conn]
                if conn in failed or deadline <= now:
                    release(conn)
                    continue
                try:
                    data = conn.recv(min(65536, remaining))
                except BlockingIOError:
                    continue
                except OSError:
                    data = b''
                remaining -= len(data)
                if not data or remaining <= 0:
                    release(conn)
                else:
                    active[conn][1] = remaining
        for conn in list(active):
            release(conn)
        while True:
            try:
                conn = self._pending.get_nowait()
            except queue.Empty:
                break
            with suppress(OSError):
                conn.close()
            self._slots.release()
