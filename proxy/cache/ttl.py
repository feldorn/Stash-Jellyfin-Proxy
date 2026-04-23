"""TTL cache — compute-on-miss with per-entry expiry."""
import threading
import time


class TTLCache:
    """Compute-on-miss cache with per-entry TTL in seconds.

    Usage:
        cache = TTLCache(ttl_seconds=30)
        value = cache.get("stash_up", producer=lambda: check_stash_connection())

    The producer runs OUTSIDE the internal lock so a slow producer never
    blocks concurrent reads against other keys.
    """
    def __init__(self, ttl_seconds: float):
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        self._data = {}  # key -> (expires_at_monotonic, value)

    def get(self, key, producer):
        """Return the cached value for `key`, invoking `producer()` on miss
        or when the entry has expired."""
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        value = producer()
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
        return value

    def invalidate(self, key=None):
        """Drop a single key or clear the entire cache."""
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)
