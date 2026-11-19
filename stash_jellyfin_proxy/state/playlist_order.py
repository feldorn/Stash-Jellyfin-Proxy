"""Persisted playlist ordering (issue #29).

Stash tag memberships are unordered, so a playlist's user-visible order is
kept here as ``{tag_id: [scene_id, ...]}`` in ``playlist_order.json`` next
to the config file (same location rule as proxy_stats.json).

The stored list is advisory, not authoritative: the Stash tag decides
*which* scenes are in the playlist; this file only decides their order.
``apply_order()`` reconciles the two on every read:

  1. scenes in the stored list that still carry the tag, in stored order;
  2. then any tagged scenes the file doesn't know about (tagged directly in
     Stash, or added before this feature existed), by ascending scene id.

Stale ids (tag removed in Stash) are dropped silently. The result is
deterministic for a given tag membership + file state.
"""
import json
import logging
import os
import threading

from stash_jellyfin_proxy import runtime

logger = logging.getLogger("stash-jellyfin-proxy")

_lock = threading.Lock()
_orders: dict = {}
_loaded_from: str = ""


def _order_file() -> str:
    cf = runtime.CONFIG_FILE or ""
    base = os.path.dirname(cf) if cf else "."
    return os.path.join(base, "playlist_order.json")


def _ensure_loaded() -> None:
    """Lazy-load on first use, and reload if CONFIG_FILE moved."""
    global _orders, _loaded_from
    path = _order_file()
    if _loaded_from == path:
        return
    _orders = {}
    _loaded_from = path
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            _orders = {
                str(k): [str(x) for x in v]
                for k, v in raw.items() if isinstance(v, list)
            }
    except Exception as e:
        logger.warning(f"playlist_order: could not read {path}: {e}")


def _save() -> None:
    path = _order_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_orders, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"playlist_order: could not write {path}: {e}")


def _id_key(sid: str):
    return (0, int(sid)) if sid.isdigit() else (1, sid)


def apply_order(tag_id: str, scene_ids) -> list:
    """Return ``scene_ids`` (the playlist's current members) in playlist order."""
    members = [str(s) for s in scene_ids]
    member_set = set(members)
    with _lock:
        _ensure_loaded()
        stored = list(_orders.get(str(tag_id), []))
    ordered = []
    seen = set()
    for sid in stored:
        if sid in member_set and sid not in seen:
            ordered.append(sid)
            seen.add(sid)
    extras = sorted((s for s in member_set if s not in seen), key=_id_key)
    return ordered + extras


def append(tag_id: str, scene_ids) -> None:
    """Record newly added scenes at the end of the playlist (dedup'd)."""
    with _lock:
        _ensure_loaded()
        cur = _orders.setdefault(str(tag_id), [])
        for sid in (str(s) for s in scene_ids):
            if sid in cur:
                cur.remove(sid)
            cur.append(sid)
        _save()


def remove(tag_id: str, scene_ids) -> None:
    with _lock:
        _ensure_loaded()
        drop = {str(s) for s in scene_ids}
        cur = _orders.get(str(tag_id))
        if cur is None:
            return
        _orders[str(tag_id)] = [s for s in cur if s not in drop]
        _save()


def move(tag_id: str, current_members, scene_id: str, new_index: int) -> bool:
    """Move ``scene_id`` to ``new_index`` within the playlist.

    ``current_members`` is the live tag membership from Stash; the full
    reconciled order is materialized and persisted so later reads are
    stable. Returns False if the scene isn't in the playlist."""
    scene_id = str(scene_id)
    order = apply_order(tag_id, current_members)
    if scene_id not in order:
        return False
    order.remove(scene_id)
    new_index = max(0, min(int(new_index), len(order)))
    order.insert(new_index, scene_id)
    with _lock:
        _ensure_loaded()
        _orders[str(tag_id)] = order
        _save()
    return True


def forget(tag_id: str) -> None:
    """Drop all ordering for a deleted playlist."""
    with _lock:
        _ensure_loaded()
        if _orders.pop(str(tag_id), None) is not None:
            _save()


def _reset_for_tests() -> None:
    global _orders, _loaded_from
    with _lock:
        _orders = {}
        _loaded_from = ""
