"""Proxy statistics — persistent counters and play-count log.

Module-level state:
    _proxy_stats       : the stats dict (mutate in place; NEVER reassign)
    _stats_dirty       : flag set by every mutation path; cleared by save()
    _stats_last_save   : monotonic timestamp of last disk write
    _play_cooldowns    : in-memory (scene_id, client_ip) → cooldown timestamp

Callers in the monolith import this module and access state via
attribute syntax (`stats._proxy_stats["total_streams"] += 1`) so every
reader sees the current value even after hot-path writes. Reassigning
_proxy_stats would break that — use `reset_stats()` to clear in place.
"""
import json
import logging
import os
import time
from typing import Optional

from proxy import runtime

logger = logging.getLogger("stash-jellyfin-proxy")

# Play-count cooldown: duration + buffer to avoid double-counting rapid
# start/stop cycles on the same client.
PLAY_COOLDOWN_BUFFER = 1800  # 30 minutes

# Stats file path resolved at import time using runtime.CONFIG_FILE if set,
# else the CWD. Late binding via _stats_file() to support bootstraps that
# set CONFIG_FILE after this module imports.
def _stats_file() -> str:
    cf = runtime.CONFIG_FILE or ""
    base = os.path.dirname(cf) if cf else "."
    return os.path.join(base, "proxy_stats.json")


_proxy_stats = {
    "total_streams": 0,
    "streams_today": 0,
    "streams_today_date": "",
    "unique_ips_today": [],
    "auth_success": 0,
    "auth_failed": 0,
    "play_counts": {},
}
_stats_dirty: bool = False
_stats_last_save: float = 0.0
_play_cooldowns = {}


def load_proxy_stats() -> None:
    """Load stats from JSON file. Merges keys into existing dict so
    reference identity is preserved (important since callers hold the
    dict by reference via `stats._proxy_stats`)."""
    path = _stats_file()
    if not os.path.isfile(path):
        return
    try:
        with open(path, 'r') as f:
            loaded = json.load(f)
        for key in _proxy_stats:
            if key in loaded:
                _proxy_stats[key] = loaded[key]
        logger.debug(f"Loaded proxy stats from {path}")
    except Exception as e:
        logger.warning(f"Could not load proxy stats: {e}")


def save_proxy_stats() -> None:
    """Flush in-memory stats to disk."""
    global _stats_dirty, _stats_last_save
    path = _stats_file()
    try:
        with open(path, 'w') as f:
            json.dump(_proxy_stats, f, indent=2)
        _stats_dirty = False
        _stats_last_save = time.time()
        logger.debug(f"Saved proxy stats to {path}")
    except Exception as e:
        logger.warning(f"Could not save proxy stats: {e}")


def maybe_save_stats() -> None:
    """Save stats if dirty and at least 60s since last save."""
    if _stats_dirty and (time.time() - _stats_last_save) > 60:
        save_proxy_stats()


def mark_dirty() -> None:
    """Flag stats as needing persistence. Called after any in-place
    mutation by external callers (middleware, endpoints)."""
    global _stats_dirty
    _stats_dirty = True


def reset_stats() -> None:
    """Clear counters to initial values. Reference identity of
    _proxy_stats is preserved (we clear + update rather than reassign)."""
    global _stats_dirty
    _proxy_stats.clear()
    _proxy_stats.update({
        "total_streams": 0,
        "streams_today": 0,
        "streams_today_date": time.strftime("%Y-%m-%d"),
        "unique_ips_today": [],
        "auth_success": 0,
        "auth_failed": 0,
        "play_counts": {},
    })
    _play_cooldowns.clear()
    _stats_dirty = True


def reset_daily_stats_if_needed() -> None:
    """Reset daily counters if the date has changed."""
    today = time.strftime("%Y-%m-%d")
    if _proxy_stats["streams_today_date"] != today:
        _proxy_stats["streams_today"] = 0
        _proxy_stats["streams_today_date"] = today
        _proxy_stats["unique_ips_today"] = []


def record_play_count(scene_id: str, title: str, performer: str, client_ip: str, duration: float = 0) -> None:
    """Record a play count for the Top Played list with duration-based
    cooldown. Separate from stream counting (which handles start/stop
    boundaries)."""
    global _stats_dirty

    safe_duration = max(0, float(duration or 0))
    cooldown_key = (scene_id, client_ip)
    cooldown_seconds = safe_duration + PLAY_COOLDOWN_BUFFER
    now = time.time()

    should_count_play = True
    if cooldown_key in _play_cooldowns:
        last_play = _play_cooldowns[cooldown_key]
        elapsed = now - last_play["timestamp"]
        if elapsed < last_play["cooldown_seconds"]:
            should_count_play = False
            remaining = int(last_play["cooldown_seconds"] - elapsed)
            logger.debug(f"Play cooldown active for {scene_id} from {client_ip} ({remaining}s remaining)")

    if should_count_play:
        _play_cooldowns[cooldown_key] = {
            "timestamp": now,
            "cooldown_seconds": cooldown_seconds,
        }

        if scene_id not in _proxy_stats["play_counts"]:
            _proxy_stats["play_counts"][scene_id] = {
                "count": 0,
                "title": title,
                "performer": performer,
                "last_played": 0,
            }

        _proxy_stats["play_counts"][scene_id]["count"] += 1
        _proxy_stats["play_counts"][scene_id]["title"] = title
        _proxy_stats["play_counts"][scene_id]["performer"] = performer
        _proxy_stats["play_counts"][scene_id]["last_played"] = now

        cooldown_mins = int(cooldown_seconds / 60)
        logger.debug(f"Play counted for {scene_id} from {client_ip} (cooldown: {cooldown_mins}min)")

    _stats_dirty = True
    maybe_save_stats()


def record_auth_attempt(success: bool) -> None:
    """Record an authentication attempt."""
    global _stats_dirty
    if success:
        _proxy_stats["auth_success"] += 1
    else:
        _proxy_stats["auth_failed"] += 1
    _stats_dirty = True


def get_top_played_scenes(limit: int = 5) -> list:
    """Return the top N most-played scenes by count."""
    play_counts = _proxy_stats.get("play_counts", {})
    sorted_scenes = sorted(
        play_counts.items(),
        key=lambda x: x[1].get("count", 0),
        reverse=True,
    )[:limit]
    return [
        {
            "scene_id": scene_id,
            "title": info.get("title", scene_id),
            "performer": info.get("performer", ""),
            "count": info.get("count", 0),
        }
        for scene_id, info in sorted_scenes
    ]


def get_proxy_stats() -> dict:
    """Snapshot stats for the Web UI dashboard."""
    reset_daily_stats_if_needed()
    return {
        "total_streams": _proxy_stats["total_streams"],
        "streams_today": _proxy_stats["streams_today"],
        "unique_ips_today": len(_proxy_stats["unique_ips_today"]),
        "auth_success": _proxy_stats["auth_success"],
        "auth_failed": _proxy_stats["auth_failed"],
        "top_played": get_top_played_scenes(5),
    }
