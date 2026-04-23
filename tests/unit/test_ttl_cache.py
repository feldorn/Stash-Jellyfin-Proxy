"""Unit tests for the TTL cache helper."""
import ast
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROXY_SRC = REPO_ROOT / "stash_jellyfin_proxy.py"


def _extract_ttl_cache():
    """Pull just the TTLCache class out of the source so tests don't have
    to import the whole proxy module."""
    tree = ast.parse(PROXY_SRC.read_text())
    cls_node = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "TTLCache"
    )
    src = ast.get_source_segment(PROXY_SRC.read_text(), cls_node)
    module_globals = {"time": time, "threading": __import__("threading")}
    exec(src, module_globals)
    return module_globals["TTLCache"]


TTLCache = _extract_ttl_cache()


def test_miss_then_hit():
    cache = TTLCache(ttl_seconds=60)
    calls = []
    def produce():
        calls.append(1)
        return "x"
    assert cache.get("k", produce) == "x"
    assert cache.get("k", produce) == "x"
    assert calls == [1]  # producer only invoked once


def test_distinct_keys_have_distinct_values():
    cache = TTLCache(ttl_seconds=60)
    assert cache.get("a", lambda: 1) == 1
    assert cache.get("b", lambda: 2) == 2


def test_expiration_triggers_reproduce():
    cache = TTLCache(ttl_seconds=0.1)
    calls = []
    def produce():
        calls.append(1)
        return len(calls)
    assert cache.get("k", produce) == 1
    time.sleep(0.2)
    assert cache.get("k", produce) == 2
    assert len(calls) == 2


def test_invalidate_single_key():
    cache = TTLCache(ttl_seconds=60)
    calls = []
    def produce():
        calls.append(1)
        return len(calls)
    assert cache.get("k", produce) == 1
    cache.invalidate("k")
    assert cache.get("k", produce) == 2


def test_invalidate_all():
    cache = TTLCache(ttl_seconds=60)
    cache.get("a", lambda: 1)
    cache.get("b", lambda: 2)
    cache.invalidate()
    calls = []
    def produce():
        calls.append(1)
        return "fresh"
    assert cache.get("a", produce) == "fresh"
    assert cache.get("b", produce) == "fresh"
    assert len(calls) == 2


def test_producer_not_held_under_lock():
    """A slow producer must not block concurrent reads on other keys."""
    import threading
    cache = TTLCache(ttl_seconds=60)

    slow_entered = threading.Event()
    slow_may_return = threading.Event()

    def slow():
        slow_entered.set()
        slow_may_return.wait(timeout=5)
        return "slow"

    def fast():
        return "fast"

    t = threading.Thread(target=lambda: cache.get("s", slow))
    t.start()
    slow_entered.wait(timeout=5)
    # While "slow" is still producing, a read for a different key must
    # succeed immediately — the cache holds no lock during producer calls.
    assert cache.get("f", fast) == "fast"
    slow_may_return.set()
    t.join(timeout=5)
    assert cache.get("s", lambda: "should-not-be-called") == "slow"
