import threading
from collections import OrderedDict
from typing import Any, Callable, Optional


class LRUCache:
    """Thread-safe LRU cache used by a request router."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        if key in self._data:
            value = self._data[key]
            self._data.move_to_end(key)
            return value
        return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            if len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def get_or_load(self, key: str, loader: Callable[[str], Any]) -> Any:
        hit = self.get(key)
        if hit is not None:
            return hit
        value = loader(key)
        self.put(key, value)
        return value

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def snapshot(self) -> dict:
        return dict(self._data)


class Router:
    def __init__(self, cache: LRUCache):
        self.cache = cache
        self.routes = {}
        self._seq = 0

    def register(self, prefix: str, target: str) -> None:
        self._seq += 1
        self.routes[prefix] = (target, self._seq)

    def resolve(self, path: str) -> Optional[str]:
        entry = self.cache.get_or_load(path, self._load_target)
        return entry

    def _load_target(self, path: str) -> Optional[str]:
        best = None
        for prefix, (target, seq) in self.routes.items():
            if path.startswith(prefix):
                if best is None or seq > self.routes[best][1]:
                    best = prefix
        return self.routes[best][0] if best else None
