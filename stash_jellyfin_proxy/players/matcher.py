"""User-Agent → Profile resolution + UA capture log.

Design §6.1: substring-only matching (no regex), case-insensitive.
First profile whose user_agent_match is a case-insensitive substring of
the UA wins. [player.default] is the fallback.

Every unique UA seen gets recorded to state/ua_log.json (timestamp,
matched profile name, first-seen). The Web UI Players tab (Phase 5B)
consumes this log to surface Connected Players.
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.players.profiles import Profile, hardcoded_default

logger = logging.getLogger("stash-jellyfin-proxy")

# Lock guards both the in-memory cache and the json file write. Writes are
# cheap (one entry per unique UA per 7 days) so a single lock is fine.
_lock = threading.Lock()
_ua_cache: Dict[str, Profile] = {}
_ua_log: Dict[str, Dict] = {}   # ua -> {profile, first_seen, last_seen}
_ua_log_loaded = False


def _ua_log_path() -> Path:
    log_dir = getattr(runtime, "LOG_DIR", None) or "."
    return Path(log_dir) / "ua_log.json"


def _load_log_once() -> None:
    global _ua_log_loaded
    if _ua_log_loaded:
        return
    path = _ua_log_path()
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                _ua_log.update(json.load(f))
    except Exception as e:
        logger.warning(f"Could not load {path}: {e}")
    _ua_log_loaded = True


def _save_log() -> None:
    path = _ua_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(_ua_log, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.warning(f"Could not write {path}: {e}")


def resolve_profile(user_agent: str) -> Profile:
    """Return the Profile matching this UA. Falls back to [player.default]
    if no profile has a matching user_agent_match. Uses a per-UA cache so
    repeated requests from the same client don't re-scan the profile list."""
    ua = user_agent or ""
    profiles = getattr(runtime, "PLAYER_PROFILES", None)
    if not profiles:
        return hardcoded_default()

    with _lock:
        cached = _ua_cache.get(ua)
        if cached is not None:
            # Update last_seen without re-logging; cheap in-memory bump.
            entry = _ua_log.get(ua)
            if entry is not None:
                entry["last_seen"] = time.time()
            return cached

    ua_lower = ua.lower()
    chosen = profiles[-1]  # default is last per load_profiles contract
    for profile in profiles[:-1]:
        match = profile.user_agent_match
        if match and match.lower() in ua_lower:
            chosen = profile
            break

    with _lock:
        _ua_cache[ua] = chosen
        _load_log_once()
        now = time.time()
        if ua not in _ua_log:
            _ua_log[ua] = {
                "profile": chosen.name,
                "first_seen": now,
                "last_seen": now,
            }
            logger.info(f"📱 New client UA matched to profile '{chosen.name}': {ua[:120]}")
            _save_log()
        else:
            _ua_log[ua]["last_seen"] = now
            # Profile reassignment (e.g., user edited config) — update log.
            if _ua_log[ua].get("profile") != chosen.name:
                _ua_log[ua]["profile"] = chosen.name
                _save_log()
    return chosen


def resolve_from_request(request) -> Profile:
    """Convenience wrapper — pulls the User-Agent header from a Starlette
    request and resolves."""
    ua = request.headers.get("user-agent", "") if request is not None else ""
    return resolve_profile(ua)


def ua_log_snapshot() -> Dict[str, Dict]:
    """Return a shallow copy of the current UA log for the Web UI."""
    with _lock:
        _load_log_once()
        return dict(_ua_log)
