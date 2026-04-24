"""Web UI handlers — the shell HTML and the /api/* JSON endpoints the
dashboard polls.

Most handlers read or mutate state that already lives in stash_jellyfin_proxy.runtime
or stash_jellyfin_proxy.state. The one remaining holdout is ui_api_config, which still
sits in the monolith because it mutates ~40 module-level config names
live; that one will move once the monolith retires.
"""
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from starlette.responses import JSONResponse, Response

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.stash.client import check_stash_connection_cached, stash_query
from stash_jellyfin_proxy.state import stats as _stats
from stash_jellyfin_proxy.state import streams as _streams

logger = logging.getLogger("stash-jellyfin-proxy")

# Load the Web UI shell HTML once at import time — same lifetime as the
# previous inline WEB_UI_HTML triple-quoted string.
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
_WEB_UI_HTML = _TEMPLATE_PATH.read_text() if _TEMPLATE_PATH.is_file() else ""


async def ui_index(request):
    """Serve the Web UI shell."""
    html = _WEB_UI_HTML.replace("{{SERVER_NAME}}", runtime.SERVER_NAME)
    return Response(html, media_type="text/html")


async def ui_api_status(request):
    """Return proxy status. Offloads the Stash health probe to a thread
    so the event loop keeps serving during slow Stash responses."""
    await asyncio.to_thread(check_stash_connection_cached)
    start_time = runtime.PROXY_START_TIME
    uptime_seconds = int(time.time() - start_time) if start_time else 0
    return JSONResponse({
        "running": runtime.PROXY_RUNNING,
        "version": "v6.02",
        "proxyBind": runtime.PROXY_BIND,
        "proxyPort": runtime.PROXY_PORT,
        "uptime": uptime_seconds,
        "stashConnected": runtime.STASH_CONNECTED,
        "stashVersion": runtime.STASH_VERSION,
        "stashUrl": runtime.STASH_URL,
        "migrationPerformed": bool(getattr(runtime, "MIGRATION_PERFORMED", False)),
        "migrationLog": list(getattr(runtime, "MIGRATION_LOG", []) or []),
    })


async def ui_api_logs(request):
    """Return the last N log entries (default 100)."""
    limit = int(request.query_params.get("limit", 100))
    entries = []

    log_path = os.path.join(runtime.LOG_DIR, runtime.LOG_FILE) if runtime.LOG_DIR else runtime.LOG_FILE
    if os.path.isfile(log_path):
        try:
            with open(log_path, 'r') as f:
                lines = f.readlines()
                for line in lines[-limit:]:
                    line = line.strip()
                    if not line:
                        continue
                    # Parse:  2025-12-03 12:08:28,115 - stash-jellyfin-proxy - INFO - msg
                    parts = line.split(" - ", 3)
                    if len(parts) >= 4:
                        entries.append({"timestamp": parts[0], "level": parts[2], "message": parts[3]})
                    else:
                        entries.append({"timestamp": "", "level": "INFO", "message": line})
        except Exception:
            pass

    return JSONResponse({"entries": entries, "logPath": log_path})


async def ui_api_streams(request):
    """Return streams active in the last 5 minutes for the dashboard."""
    streams = []
    now = time.time()
    for scene_id, info in _streams._active_streams.items():
        if now - info.get("last_seen", 0) < 300:
            streams.append({
                "id": scene_id,
                "title": info.get("title", scene_id),
                "performer": info.get("performer", ""),
                "started": info.get("started", 0),
                "lastSeen": info.get("last_seen", 0),
                "user": info.get("user", runtime.SJS_USER),
                "clientIp": info.get("client_ip", "unknown"),
                "clientType": info.get("client_type", "unknown"),
            })
    return JSONResponse({"streams": streams})


async def ui_api_stats(request):
    """Return Stash library counts + proxy usage counters."""
    stash_stats = {"scenes": 0, "performers": 0, "studios": 0, "tags": 0, "groups": 0}
    try:
        query = """query {
            stats {
                scene_count
                performer_count
                studio_count
                tag_count
                movie_count
            }
        }"""
        result = await stash_query(query, {})
        stats_data = result.get("data", {}).get("stats", {})
        stash_stats = {
            "scenes": stats_data.get("scene_count", 0),
            "performers": stats_data.get("performer_count", 0),
            "studios": stats_data.get("studio_count", 0),
            "tags": stats_data.get("tag_count", 0),
            "groups": stats_data.get("movie_count", 0),
        }
    except Exception as e:
        logger.debug(f"Could not fetch Stash stats: {e}")

    return JSONResponse({"stash": stash_stats, "proxy": _stats.get_proxy_stats()})


async def ui_api_stats_reset(request):
    """Reset all proxy statistics."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    logger.info("Statistics reset requested via Web UI")
    _stats.reset_stats()
    _stats.save_proxy_stats()
    return JSONResponse({"success": True, "message": "Statistics reset"})


async def ui_api_clear_cache(request):
    """Clear in-memory caches: image bytes, library-card artwork, genre
    allow-list, filter-panel cache. Everything rebuilds on next request."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    cleared = []
    try:
        runtime.IMAGE_CACHE.clear()
        cleared.append("image_cache")
    except Exception:
        pass
    try:
        from stash_jellyfin_proxy.endpoints.images import _LIBRARY_CARD_CACHE
        _LIBRARY_CARD_CACHE.clear()
        cleared.append("library_card_cache")
    except Exception:
        pass
    try:
        from stash_jellyfin_proxy.mapping.genre import invalidate_allowed_cache
        invalidate_allowed_cache()
        cleared.append("genre_allow_cache")
    except Exception:
        pass
    try:
        from stash_jellyfin_proxy.endpoints.views import _NEXTUP_CACHE
        _NEXTUP_CACHE["payload"] = None
        _NEXTUP_CACHE["expires"] = 0
        cleared.append("nextup_cache")
    except Exception:
        pass
    try:
        runtime.SERIES_SCENE_CACHE.clear()
        cleared.append("series_scene_cache")
    except Exception:
        pass
    logger.info(f"Cache cleared via Web UI: {', '.join(cleared)}")
    return JSONResponse({"success": True, "cleared": cleared})


async def ui_api_download_config(request):
    """Return the raw config file so the user can back it up locally."""
    path = runtime.CONFIG_FILE
    if not path or not os.path.isfile(path):
        return JSONResponse({"error": "config file not found"}, status_code=404)
    try:
        with open(path, "r") as f:
            data = f.read()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    filename = os.path.basename(path)
    return Response(
        data,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def ui_api_stash_test(request):
    """Live-probe Stash with candidate connection settings from the Connection
    tab. Does NOT touch runtime.STASH_URL/STASH_API_KEY — lets the user
    validate credentials before saving + restarting."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        body = await request.json()
    except Exception:
        body = {}

    url = (body.get("STASH_URL") or runtime.STASH_URL or "").rstrip("/")
    gq_path = body.get("STASH_GRAPHQL_PATH") or runtime.STASH_GRAPHQL_PATH or "/graphql"
    api_key = body.get("STASH_API_KEY") or runtime.STASH_API_KEY
    verify_tls = bool(body.get("STASH_VERIFY_TLS", runtime.STASH_VERIFY_TLS))

    if not url:
        return JSONResponse({"ok": False, "error": "STASH_URL is empty"})

    full_url = url + (gq_path if gq_path.startswith("/") else "/" + gq_path)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["ApiKey"] = api_key

    def _probe():
        import httpx
        try:
            with httpx.Client(verify=verify_tls, headers=headers, timeout=8, follow_redirects=True) as client:
                resp = client.post(full_url, json={"query": "{ version { version } }"})
                resp.raise_for_status()
                data = resp.json()
                version = (data.get("data") or {}).get("version", {}).get("version")
                if not version:
                    errs = data.get("errors") or []
                    msg = errs[0].get("message") if errs else "no version in response"
                    return {"ok": False, "error": f"GraphQL returned no version: {msg}"}
                return {"ok": True, "version": version}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    result = await asyncio.to_thread(_probe)
    return JSONResponse(result)


async def ui_api_players_ua_log(request):
    """Return the UA log snapshot for the Players tab. Adds a computed
    last_seen_age seconds value and the resolved profile name."""
    from stash_jellyfin_proxy.players.matcher import ua_log_snapshot, resolve_profile
    snap = ua_log_snapshot()
    now = time.time()
    cutoff = now - 7 * 24 * 60 * 60   # last 7 days per design
    out = []
    for ua, info in snap.items():
        last_seen = info.get("last_seen", 0) or 0
        if last_seen < cutoff:
            continue
        profile = info.get("profile") or resolve_profile(ua).name
        out.append({
            "userAgent": ua,
            "firstSeen": info.get("first_seen", 0),
            "lastSeen": last_seen,
            "ageSeconds": max(0, int(now - last_seen)),
            "profile": profile,
        })
    out.sort(key=lambda e: -e["lastSeen"])
    return JSONResponse({"entries": out, "now": int(now)})


def _profile_dict(p):
    return {
        "name": p.name,
        "userAgentMatch": getattr(p, "user_agent_match", ""),
        "performerType": getattr(p, "performer_type", "BoxSet"),
        "posterFormat": getattr(p, "poster_format", "landscape"),
        "isDefault": p.name == "default",
    }


async def ui_api_players_profiles(request):
    """List current [player.*] profiles. The list is computed at bootstrap
    and re-derived by _reload_player_profiles() after any edit."""
    profiles = list(getattr(runtime, "PLAYER_PROFILES", []) or [])
    # Pin 'default' last for display parity with matching order.
    profiles.sort(key=lambda p: (p.name == "default", p.name))
    return JSONResponse({"profiles": [_profile_dict(p) for p in profiles]})


def _reload_player_profiles():
    """Rebuild runtime.PLAYER_PROFILES from runtime.config_sections. Called
    after a profile create/update/delete writes the config file and updates
    the in-memory sections dict."""
    from stash_jellyfin_proxy.players.profiles import load_profiles
    runtime.PLAYER_PROFILES = load_profiles(runtime.config_sections or {})


def _rewrite_player_sections(updated_sections):
    """Rewrite the [player.*] portion of runtime.CONFIG_FILE with the
    supplied dict of section_name → body(dict). Non-player lines are
    preserved in place; the player block is replaced wholesale at the
    first [player.*] line found (or appended if none exist)."""
    path = runtime.CONFIG_FILE
    if not path or not os.path.isfile(path):
        raise RuntimeError("CONFIG_FILE is not set or does not exist")
    with open(path, "r") as f:
        lines = f.readlines()

    out = []
    in_player = False
    first_player_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[player."):
            if first_player_idx is None:
                first_player_idx = len(out)
            in_player = True
            continue  # drop the header; we'll rewrite all player blocks at first_player_idx
        if in_player:
            if stripped.startswith("[") and stripped.endswith("]"):
                # Entering a non-player section — stop skipping.
                in_player = False
                out.append(line)
                continue
            if stripped == "" or stripped.startswith("#"):
                continue   # drop blanks/comments inside player blocks
            if "=" in stripped:
                continue   # drop key=value inside player blocks
            in_player = False
            out.append(line)
        else:
            out.append(line)

    # Compose the new player block.
    block = []
    if first_player_idx is None:
        block.append("\n# ==== Player profiles ====\n")
    # Ensure stable ordering: non-default profiles alphabetically, then default last.
    names = sorted([n for n in updated_sections.keys() if n != "player.default"])
    if "player.default" in updated_sections:
        names.append("player.default")
    for section in names:
        block.append(f"[{section}]\n")
        body = updated_sections[section]
        for key in ("user_agent_match", "performer_type", "poster_format"):
            if key in body and body[key] != "":
                block.append(f"{key} = {body[key]}\n")
        block.append("\n")

    if first_player_idx is None:
        out.extend(block)
    else:
        out[first_player_idx:first_player_idx] = block

    with open(path, "w") as f:
        f.writelines(out)

    # Mirror into the in-memory sections dict and reload profiles.
    # Drop every current [player.*] section, then apply the new ones.
    sections = runtime.config_sections or {}
    for k in list(sections.keys()):
        if k.startswith("player."):
            del sections[k]
    for k, v in updated_sections.items():
        sections[k] = dict(v)
    runtime.config_sections = sections
    _reload_player_profiles()


async def ui_api_players_save_profile(request):
    """Create or update a player profile. Body: {name, userAgentMatch,
    performerType, posterFormat}. The profile name is validated and
    lowercased; the [player.default] entry cannot be renamed."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    name = str(body.get("name", "")).strip().lower()
    if not name or not all(c.isalnum() or c == "_" for c in name):
        return JSONResponse({"error": "Profile name must be lowercase alphanumerics/underscore"}, status_code=400)

    ua_match = str(body.get("userAgentMatch", "")).strip()
    perf_type = str(body.get("performerType", "BoxSet")).strip()
    poster = str(body.get("posterFormat", "landscape")).strip()
    if perf_type not in ("Person", "BoxSet"):
        perf_type = "BoxSet"
    if poster not in ("portrait", "landscape", "original"):
        poster = "landscape"

    # Compose the new section set: existing + this one (overwritten).
    current_sections = runtime.config_sections or {}
    updated = {k: dict(v) for k, v in current_sections.items() if k.startswith("player.")}
    updated[f"player.{name}"] = {
        "user_agent_match": ua_match if name != "default" else "",
        "performer_type": perf_type,
        "poster_format": poster,
    }
    try:
        await asyncio.to_thread(_rewrite_player_sections, updated)
    except Exception as e:
        logger.exception("Failed to save player profile")
        return JSONResponse({"error": str(e)}, status_code=500)
    logger.info(f"Player profile saved via Web UI: {name}")
    return JSONResponse({"success": True, "name": name})


async def ui_api_players_delete_profile(request):
    """Delete a player profile. The 'default' profile cannot be deleted."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    name = str(body.get("name", "")).strip().lower()
    if name == "default" or not name:
        return JSONResponse({"error": "cannot delete default profile"}, status_code=400)

    current_sections = runtime.config_sections or {}
    updated = {k: dict(v) for k, v in current_sections.items() if k.startswith("player.")}
    key = f"player.{name}"
    if key not in updated:
        return JSONResponse({"error": "profile not found"}, status_code=404)
    del updated[key]
    try:
        await asyncio.to_thread(_rewrite_player_sections, updated)
    except Exception as e:
        logger.exception("Failed to delete player profile")
        return JSONResponse({"error": str(e)}, status_code=500)
    logger.info(f"Player profile deleted via Web UI: {name}")
    return JSONResponse({"success": True})


async def ui_api_restart(request):
    """Request a proxy restart. Sets runtime-level flag + event; the
    bootstrap loop picks those up and exits for the supervisor to relaunch."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    logger.info("Restart requested via Web UI")
    # The shutdown event is published to runtime.SHUTDOWN_EVENT by the
    # main bootstrap so both the UI handler and the signal handler can
    # reach it through the same namespace.
    ev = getattr(runtime, "SHUTDOWN_EVENT", None)
    setattr(runtime, "RESTART_REQUESTED", True)

    async def delayed_shutdown():
        await asyncio.sleep(1)
        logger.info("Shutting down for restart...")
        if ev is not None:
            ev.set()

    asyncio.create_task(delayed_shutdown())
    return JSONResponse({"success": True, "message": "Restarting..."})


async def ui_api_auth_config(request):
    """Authenticate a Web UI config change (when REQUIRE_AUTH_FOR_CONFIG=true)."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    try:
        data = await request.json()
        password = data.get("password", "")
        logger.debug(f"Auth attempt: input len={len(password)}, expected len={len(runtime.SJS_PASSWORD)}")
        if password.strip() == runtime.SJS_PASSWORD.strip():
            logger.info("Config authentication successful")
            return JSONResponse({"success": True})
        logger.warning("Config authentication failed - password mismatch")
        return JSONResponse({"success": False, "error": "Invalid password"})
    except Exception as e:
        logger.error(f"Config authentication error: {e}")
        return JSONResponse({"success": False, "error": str(e)})


# --- Config read/write endpoint (extracted from monolith) ---
import json as _json_mod  # already imported above, but explicit here for clarity
from stash_jellyfin_proxy.config.helpers import normalize_path

# Table-driven extension for new Phase 5B keys. Each tuple:
#   (config_key, runtime_attr, kind, default, live)
# kind:  "str" | "int" | "bool" | "list"
# live:  True  → applied immediately from the config write
#        False → runtime only updates on restart; the file is written now
#
# The existing per-key if/elif chain in the POST handler covers every
# pre-P5B key. This table extends that with P5B pass 4-6 keys (Libraries,
# Playback, Search) without duplicating boilerplate.
_P5B_KEYS = [
    # --- Libraries (pass 4) ---
    ("GENRE_MODE",            "GENRE_MODE",            "str",  "parent_tag", False),
    ("GENRE_PARENT_TAG",      "GENRE_PARENT_TAG",      "str",  "GENRE",      False),
    ("GENRE_TOP_N",           "GENRE_TOP_N",           "int",  25,           False),
    ("SERIES_TAG",            "SERIES_TAG",            "str",  "Series",     False),
    ("SERIES_EPISODE_PATTERNS","SERIES_EPISODE_PATTERNS","str", "",           True),
    # --- Playback (pass 5) ---
    ("POSTER_CROP_ANCHOR",    "POSTER_CROP_ANCHOR",    "str",  "center",     False),
    ("SCENES_DEFAULT_SORT",   "SCENES_DEFAULT_SORT",   "str",  "DateCreated",True),
    ("STUDIOS_DEFAULT_SORT",  "STUDIOS_DEFAULT_SORT",  "str",  "SortName",   True),
    ("PERFORMERS_DEFAULT_SORT","PERFORMERS_DEFAULT_SORT","str", "SortName",   True),
    ("GROUPS_DEFAULT_SORT",   "GROUPS_DEFAULT_SORT",   "str",  "SortName",   True),
    ("TAG_GROUPS_DEFAULT_SORT","TAG_GROUPS_DEFAULT_SORT","str", "PlayCount",  True),
    ("SAVED_FILTERS_DEFAULT_SORT","SAVED_FILTERS_DEFAULT_SORT","str","PlayCount", True),
    ("SORT_STRIP_ARTICLES",   "SORT_STRIP_ARTICLES",   "list", ["The","A","An"], True),
    ("HERO_SOURCE",           "HERO_SOURCE",           "str",  "recent",     False),
    ("HERO_MIN_RATING",       "HERO_MIN_RATING",       "int",  75,           False),
    # --- Search (pass 6) ---
    ("SEARCH_INCLUDE_SCENES",     "SEARCH_INCLUDE_SCENES",     "bool", True, True),
    ("SEARCH_INCLUDE_PERFORMERS", "SEARCH_INCLUDE_PERFORMERS", "bool", True, True),
    ("SEARCH_INCLUDE_STUDIOS",    "SEARCH_INCLUDE_STUDIOS",    "bool", True, True),
    ("SEARCH_INCLUDE_GROUPS",     "SEARCH_INCLUDE_GROUPS",     "bool", True, True),
    ("FILTER_TAGS_MAX",           "FILTER_TAGS_MAX",           "int",  50,   True),
    ("GENRE_FILTER_LOGIC",        "GENRE_FILTER_LOGIC",        "str",  "AND", True),
    ("FILTER_TAGS_WALK_HIERARCHY","FILTER_TAGS_WALK_HIERARCHY","bool", True, True),
]


def _p5b_get_value(cfg_key: str):
    """Fetch current runtime value for a P5B key in JSON-friendly form."""
    for k, attr, kind, default, _live in _P5B_KEYS:
        if k == cfg_key:
            val = getattr(runtime, attr, default)
            if kind == "list" and not isinstance(val, list):
                val = [s.strip() for s in str(val).split(",") if s.strip()]
            return val
    return None


def _p5b_coerce(kind, raw):
    """Coerce incoming JSON value → runtime-native type."""
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "yes", "1", "on")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if kind == "list":
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if str(s).strip()]
        return [s.strip() for s in str(raw).split(",") if s.strip()]
    return str(raw) if raw is not None else ""


def _p5b_stringify(kind, val):
    """Coerce runtime value → string for config-file storage / comparison."""
    if kind == "bool":
        return "true" if val else "false"
    if kind == "list":
        return ", ".join(str(s) for s in (val or []))
    return str(val)


def _p5b_apply_update(cfg_key: str, raw_value):
    """Apply a P5B key from the updates dict. Returns True if live (runtime
    was mutated now) or False if the file was written but runtime won't
    see it until restart."""
    for k, attr, kind, _default, live in _P5B_KEYS:
        if k != cfg_key:
            continue
        coerced = _p5b_coerce(kind, raw_value)
        if coerced is None:
            return False
        if live:
            setattr(runtime, attr, coerced)
        return live
    return False


def _p5b_apply_default(cfg_key: str):
    """Reset a P5B key to its declared default (for commented-out lines)."""
    for k, attr, kind, default, live in _P5B_KEYS:
        if k != cfg_key:
            continue
        val = list(default) if kind == "list" and isinstance(default, list) else default
        if live:
            setattr(runtime, attr, val)
        return live
    return False


async def ui_api_config(request):
    """Get or set configuration."""
    # Declare globals at top of function (required before any reference)

    if request.method == "GET":
        return JSONResponse({
            "config": {
                "STASH_URL": runtime.STASH_URL,
                "STASH_API_KEY": "*" * min(len(runtime.STASH_API_KEY), 20) if runtime.STASH_API_KEY else "",
                "STASH_GRAPHQL_PATH": runtime.STASH_GRAPHQL_PATH,
                "STASH_VERIFY_TLS": runtime.STASH_VERIFY_TLS,
                "PROXY_BIND": runtime.PROXY_BIND,
                "PROXY_PORT": runtime.PROXY_PORT,
                "UI_PORT": runtime.UI_PORT,
                "SJS_USER": runtime.SJS_USER,
                "SJS_PASSWORD": "*" * min(len(runtime.SJS_PASSWORD), 10) if runtime.SJS_PASSWORD else "",
                "SERVER_ID": runtime.SERVER_ID,
                "SERVER_NAME": runtime.SERVER_NAME,
                "TAG_GROUPS": runtime.TAG_GROUPS,
                "FAVORITE_TAG": runtime.FAVORITE_TAG,
                "LATEST_GROUPS": runtime.LATEST_GROUPS,
                "BANNER_MODE": runtime.BANNER_MODE,
                "BANNER_POOL_SIZE": runtime.BANNER_POOL_SIZE,
                "BANNER_TAGS": runtime.BANNER_TAGS,
                "STASH_TIMEOUT": runtime.STASH_TIMEOUT,
                "STASH_RETRIES": runtime.STASH_RETRIES,
                "ENABLE_FILTERS": runtime.ENABLE_FILTERS,
                "ENABLE_IMAGE_RESIZE": runtime.ENABLE_IMAGE_RESIZE,
                "ENABLE_TAG_FILTERS": runtime.ENABLE_TAG_FILTERS,
                "ENABLE_ALL_TAGS": runtime.ENABLE_ALL_TAGS,
                "REQUIRE_AUTH_FOR_CONFIG": runtime.REQUIRE_AUTH_FOR_CONFIG,
                "IMAGE_CACHE_MAX_SIZE": runtime.IMAGE_CACHE_MAX_SIZE,
                "DEFAULT_PAGE_SIZE": runtime.DEFAULT_PAGE_SIZE,
                "MAX_PAGE_SIZE": runtime.MAX_PAGE_SIZE,
                "LOG_LEVEL": runtime.LOG_LEVEL,
                "LOG_DIR": runtime.LOG_DIR,
                "LOG_FILE": runtime.LOG_FILE,
                "LOG_MAX_SIZE_MB": runtime.LOG_MAX_SIZE_MB,
                "LOG_BACKUP_COUNT": runtime.LOG_BACKUP_COUNT,
                "BAN_THRESHOLD": runtime.BAN_THRESHOLD,
                "BAN_WINDOW_MINUTES": runtime.BAN_WINDOW_MINUTES,
                "BANNED_IPS": ", ".join(sorted(runtime.BANNED_IPS)) if runtime.BANNED_IPS else "",
                **{k: _p5b_get_value(k) for k, *_ in _P5B_KEYS},
            },
            "env_fields": runtime.env_overrides,
            "defined_fields": sorted(list(runtime.config_defined_keys))
        })
    elif request.method == "POST":
        try:
            data = await request.json()
            config_keys = [
                "STASH_URL", "STASH_API_KEY", "STASH_GRAPHQL_PATH", "STASH_VERIFY_TLS",
                "PROXY_BIND", "PROXY_PORT", "UI_PORT",
                "SJS_USER", "SJS_PASSWORD", "SERVER_ID", "SERVER_NAME",
                "TAG_GROUPS", "FAVORITE_TAG", "LATEST_GROUPS",
                "BANNER_MODE", "BANNER_POOL_SIZE", "BANNER_TAGS",
                "STASH_TIMEOUT", "STASH_RETRIES",
                "ENABLE_FILTERS", "ENABLE_IMAGE_RESIZE", "ENABLE_TAG_FILTERS", "ENABLE_ALL_TAGS", "REQUIRE_AUTH_FOR_CONFIG", "IMAGE_CACHE_MAX_SIZE",
                "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE",
                "LOG_LEVEL", "LOG_DIR", "LOG_FILE", "LOG_MAX_SIZE_MB", "LOG_BACKUP_COUNT",
                "BAN_THRESHOLD", "BAN_WINDOW_MINUTES", "BANNED_IPS",
                *[k for k, *_ in _P5B_KEYS],
            ]

            # Sensitive keys - log changes but mask values
            sensitive_keys = ["STASH_API_KEY", "SJS_PASSWORD"]

            # Read existing config file preserving all lines
            original_lines = []
            existing_values = {}  # Currently active (uncommented) values
            all_keys_in_file = set()  # Track all keys in file (commented or not)
            if os.path.isfile(runtime.CONFIG_FILE):
                with open(runtime.CONFIG_FILE, 'r') as f:
                    original_lines = f.readlines()
                    for line in original_lines:
                        stripped = line.strip()
                        if stripped and not stripped.startswith('#') and '=' in stripped:
                            key, _, value = stripped.partition('=')
                            key = key.strip()
                            existing_values[key] = value.strip().strip('"').strip("'")
                            all_keys_in_file.add(key)
                        elif stripped.startswith('#') and '=' in stripped:
                            # Track commented keys too
                            uncommented = stripped.lstrip('#').strip()
                            if '=' in uncommented:
                                key, _, _ = uncommented.partition('=')
                                all_keys_in_file.add(key.strip())

            # Get current running values to compare against
            current_running = {
                "STASH_URL": runtime.STASH_URL,
                "STASH_API_KEY": runtime.STASH_API_KEY,
                "STASH_GRAPHQL_PATH": runtime.STASH_GRAPHQL_PATH,
                "STASH_VERIFY_TLS": "true" if runtime.STASH_VERIFY_TLS else "false",
                "PROXY_BIND": runtime.PROXY_BIND,
                "PROXY_PORT": str(runtime.PROXY_PORT),
                "UI_PORT": str(runtime.UI_PORT),
                "SJS_USER": runtime.SJS_USER,
                "SJS_PASSWORD": runtime.SJS_PASSWORD,
                "SERVER_ID": runtime.SERVER_ID,
                "SERVER_NAME": runtime.SERVER_NAME,
                "TAG_GROUPS": ", ".join(runtime.TAG_GROUPS) if runtime.TAG_GROUPS else "",
                "FAVORITE_TAG": runtime.FAVORITE_TAG,
                "LATEST_GROUPS": ", ".join(runtime.LATEST_GROUPS) if runtime.LATEST_GROUPS else "",
                "BANNER_MODE": runtime.BANNER_MODE,
                "BANNER_POOL_SIZE": str(runtime.BANNER_POOL_SIZE),
                "BANNER_TAGS": ", ".join(runtime.BANNER_TAGS) if runtime.BANNER_TAGS else "",
                "STASH_TIMEOUT": str(runtime.STASH_TIMEOUT),
                "STASH_RETRIES": str(runtime.STASH_RETRIES),
                "ENABLE_FILTERS": "true" if runtime.ENABLE_FILTERS else "false",
                "ENABLE_IMAGE_RESIZE": "true" if runtime.ENABLE_IMAGE_RESIZE else "false",
                "ENABLE_TAG_FILTERS": "true" if runtime.ENABLE_TAG_FILTERS else "false",
                "ENABLE_ALL_TAGS": "true" if runtime.ENABLE_ALL_TAGS else "false",
                "REQUIRE_AUTH_FOR_CONFIG": "true" if runtime.REQUIRE_AUTH_FOR_CONFIG else "false",
                "IMAGE_CACHE_MAX_SIZE": str(runtime.IMAGE_CACHE_MAX_SIZE),
                "DEFAULT_PAGE_SIZE": str(runtime.DEFAULT_PAGE_SIZE),
                "MAX_PAGE_SIZE": str(runtime.MAX_PAGE_SIZE),
                "LOG_LEVEL": runtime.LOG_LEVEL,
                "LOG_DIR": runtime.LOG_DIR,
                "LOG_FILE": runtime.LOG_FILE,
                "LOG_MAX_SIZE_MB": str(runtime.LOG_MAX_SIZE_MB),
                "LOG_BACKUP_COUNT": str(runtime.LOG_BACKUP_COUNT),
                "BAN_THRESHOLD": str(runtime.BAN_THRESHOLD),
                "BAN_WINDOW_MINUTES": str(runtime.BAN_WINDOW_MINUTES),
                "BANNED_IPS": ", ".join(sorted(runtime.BANNED_IPS)) if runtime.BANNED_IPS else "",
                **{k: _p5b_stringify(kind, getattr(runtime, attr, default))
                   for k, attr, kind, default, _live in _P5B_KEYS},
            }

            # Default values for comparison
            defaults = {
                "STASH_URL": "https://stash:9999",
                "STASH_API_KEY": "",
                "STASH_GRAPHQL_PATH": "/graphql",
                "STASH_VERIFY_TLS": "false",
                "PROXY_BIND": "0.0.0.0",
                "PROXY_PORT": "8096",
                "UI_PORT": "8097",
                "SJS_USER": "",
                "SJS_PASSWORD": "",
                "SERVER_ID": "",
                "SERVER_NAME": "Stash Media Server",
                "TAG_GROUPS": "",
                "FAVORITE_TAG": "",
                "LATEST_GROUPS": "",
                "BANNER_MODE": "recent",
                "BANNER_POOL_SIZE": "200",
                "BANNER_TAGS": "",
                "STASH_TIMEOUT": "30",
                "STASH_RETRIES": "3",
                "ENABLE_FILTERS": "true",
                "ENABLE_IMAGE_RESIZE": "true",
                "ENABLE_TAG_FILTERS": "false",
                "ENABLE_ALL_TAGS": "false",
                "REQUIRE_AUTH_FOR_CONFIG": "false",
                "IMAGE_CACHE_MAX_SIZE": "1000",
                "DEFAULT_PAGE_SIZE": "50",
                "MAX_PAGE_SIZE": "200",
                "LOG_LEVEL": "INFO",
                "LOG_DIR": "/config",
                "LOG_FILE": "stash_jellyfin_proxy.log",
                "LOG_MAX_SIZE_MB": "10",
                "LOG_BACKUP_COUNT": "3",
                "BAN_THRESHOLD": "10",
                "BAN_WINDOW_MINUTES": "15",
                "BANNED_IPS": "",
                **{k: _p5b_stringify(kind, default) for k, _attr, kind, default, _live in _P5B_KEYS},
            }

            # Prepare new values and track which keys should be commented out (reverted to default)
            updates = {}
            comment_out = set()  # Keys to comment out (user wants to use default)

            for key in config_keys:
                if key in data:
                    value = data[key]
                    # Don't update masked passwords
                    if key in ["STASH_API_KEY", "SJS_PASSWORD"] and str(value).startswith("*"):
                        continue
                    if isinstance(value, list):
                        value = ", ".join(value)
                    elif isinstance(value, bool):
                        value = "true" if value else "false"
                    new_value = str(value)

                    # Check if value equals default
                    default_value = defaults.get(key, "")
                    is_default = (new_value == default_value)

                    # If user cleared the field (empty) and there's a non-empty default,
                    # treat this as wanting the default value
                    is_cleared_for_default = (new_value == "" and default_value != "")

                    # Check if key is currently defined (uncommented) in config file
                    is_defined_in_file = key in existing_values

                    # Compare against running value
                    running_value = current_running.get(key, "")

                    if (is_default or is_cleared_for_default) and is_defined_in_file:
                        # User cleared the field or set to default - comment out the line to use default
                        comment_out.add(key)
                    elif new_value != running_value and not is_cleared_for_default:
                        # Value changed to something non-default
                        updates[key] = new_value

            # Update lines in-place
            updated_keys = set()
            commented_keys = set()
            new_lines = []
            for line in original_lines:
                stripped = line.strip()

                # Check for uncommented key=value
                if stripped and not stripped.startswith('#') and '=' in stripped:
                    key, _, old_value = stripped.partition('=')
                    key = key.strip()
                    if key in comment_out:
                        # Comment out this line (user wants default)
                        indent = len(line) - len(line.lstrip())
                        new_lines.append(f'{" " * indent}# {stripped}\n')
                        commented_keys.add(key)
                    elif key in updates:
                        indent = len(line) - len(line.lstrip())
                        new_lines.append(f'{" " * indent}{key} = "{updates[key]}"\n')
                        updated_keys.add(key)
                    else:
                        new_lines.append(line)
                # Check for commented key=value - uncomment if value needs to change
                elif stripped.startswith('#') and '=' in stripped:
                    uncommented = stripped.lstrip('#').strip()
                    if '=' in uncommented:
                        key, _, old_value = uncommented.partition('=')
                        key = key.strip()
                        if key in updates and key not in updated_keys:
                            # Uncomment and update the value
                            indent = len(line) - len(line.lstrip())
                            new_lines.append(f'{" " * indent}{key} = "{updates[key]}"\n')
                            updated_keys.add(key)
                        else:
                            new_lines.append(line)
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            # Only add truly new keys that don't exist anywhere in the file
            for key in updates:
                if key not in updated_keys:
                    new_lines.append(f'{key} = "{updates[key]}"\n')

            # Log configuration changes
            for key, new_val in updates.items():
                old_val = current_running.get(key, "(unknown)")
                if key in sensitive_keys:
                    logger.info(f"Config changed: {key} = ******* (sensitive)")
                else:
                    logger.info(f"Config changed: {key}: \"{old_val}\" -> \"{new_val}\"")

            # Log reverted-to-default fields
            for key in commented_keys:
                old_val = existing_values.get(key, "(unknown)")
                default_val = defaults.get(key, "")
                if key in sensitive_keys:
                    logger.info(f"Config reverted to default: {key} (sensitive)")
                else:
                    logger.info(f"Config reverted to default: {key}: \"{old_val}\" -> default \"{default_val}\"")

            # Write updated config file
            with open(runtime.CONFIG_FILE, 'w') as f:
                f.writelines(new_lines)

            # Apply configuration changes immediately (where safe to do so)
            # Settings that need restart: PROXY_BIND, PROXY_PORT, UI_PORT, LOG_DIR, LOG_FILE
            # Settings that need restart: STASH_URL, STASH_API_KEY (connection settings)
            # Settings that need restart: SJS_USER, SJS_PASSWORD (auth tokens may be cached)

            applied_immediately = []
            needs_restart = []

            # Apply safe settings from updates dict
            for key, new_val in updates.items():
                if key == "TAG_GROUPS":
                    runtime.TAG_GROUPS = [t.strip() for t in new_val.split(",") if t.strip()]
                    applied_immediately.append(key)
                elif key == "FAVORITE_TAG":
                    runtime.FAVORITE_TAG = new_val.strip()
                    runtime.favorite_tag_id_cache = None
                    applied_immediately.append(key)
                elif key == "LATEST_GROUPS":
                    runtime.LATEST_GROUPS = [t.strip() for t in new_val.split(",") if t.strip()]
                    applied_immediately.append(key)
                elif key == "BANNER_MODE":
                    m = new_val.strip().lower()
                    runtime.BANNER_MODE = m if m in ("recent", "tag") else "recent"
                    applied_immediately.append(key)
                elif key == "BANNER_POOL_SIZE":
                    try:
                        runtime.BANNER_POOL_SIZE = max(1, int(new_val))
                    except ValueError:
                        runtime.BANNER_POOL_SIZE = 200
                    applied_immediately.append(key)
                elif key == "BANNER_TAGS":
                    runtime.BANNER_TAGS = [t.strip() for t in new_val.split(",") if t.strip()]
                    applied_immediately.append(key)
                elif key == "SERVER_NAME":
                    runtime.SERVER_NAME = new_val
                    applied_immediately.append(key)
                elif key == "STASH_TIMEOUT":
                    runtime.STASH_TIMEOUT = int(new_val)
                    applied_immediately.append(key)
                elif key == "STASH_RETRIES":
                    runtime.STASH_RETRIES = int(new_val)
                    applied_immediately.append(key)
                elif key == "STASH_GRAPHQL_PATH":
                    runtime.STASH_GRAPHQL_PATH = normalize_path(new_val)
                    applied_immediately.append(key)
                elif key == "STASH_VERIFY_TLS":
                    runtime.STASH_VERIFY_TLS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_FILTERS":
                    runtime.ENABLE_FILTERS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_IMAGE_RESIZE":
                    runtime.ENABLE_IMAGE_RESIZE = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_TAG_FILTERS":
                    runtime.ENABLE_TAG_FILTERS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_ALL_TAGS":
                    runtime.ENABLE_ALL_TAGS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "IMAGE_CACHE_MAX_SIZE":
                    runtime.IMAGE_CACHE_MAX_SIZE = int(new_val)
                    applied_immediately.append(key)
                elif key == "DEFAULT_PAGE_SIZE":
                    runtime.DEFAULT_PAGE_SIZE = int(new_val)
                    applied_immediately.append(key)
                elif key == "MAX_PAGE_SIZE":
                    runtime.MAX_PAGE_SIZE = int(new_val)
                    applied_immediately.append(key)
                elif key == "REQUIRE_AUTH_FOR_CONFIG":
                    runtime.REQUIRE_AUTH_FOR_CONFIG = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "LOG_LEVEL":
                    runtime.LOG_LEVEL = new_val.upper()
                    # Update logger level
                    level = getattr(logging, runtime.LOG_LEVEL, logging.INFO)
                    logger.setLevel(level)
                    for handler in logger.handlers:
                        handler.setLevel(level)
                    applied_immediately.append(key)
                elif key == "BAN_THRESHOLD":
                    runtime.BAN_THRESHOLD = int(new_val)
                    applied_immediately.append(key)
                elif key == "BAN_WINDOW_MINUTES":
                    runtime.BAN_WINDOW_MINUTES = int(new_val)
                    applied_immediately.append(key)
                elif key == "BANNED_IPS":
                    runtime.BANNED_IPS = set(ip.strip() for ip in new_val.split(",") if ip.strip())
                    applied_immediately.append(key)
                elif key in ["PROXY_BIND", "PROXY_PORT", "UI_PORT", "LOG_DIR", "LOG_FILE",
                             "STASH_URL", "STASH_API_KEY", "SJS_USER", "SJS_PASSWORD", "SERVER_ID"]:
                    needs_restart.append(key)
                elif key in {k for k, *_ in _P5B_KEYS}:
                    if _p5b_apply_update(key, new_val):
                        applied_immediately.append(key)
                    else:
                        needs_restart.append(key)

            # Apply default values for commented-out keys
            for key in commented_keys:
                default_val = defaults.get(key, "")
                if key == "TAG_GROUPS":
                    runtime.TAG_GROUPS = []
                    applied_immediately.append(key)
                elif key == "FAVORITE_TAG":
                    runtime.FAVORITE_TAG = ""
                    runtime.favorite_tag_id_cache = None
                    applied_immediately.append(key)
                elif key == "LATEST_GROUPS":
                    runtime.LATEST_GROUPS = []
                    applied_immediately.append(key)
                elif key == "BANNER_MODE":
                    runtime.BANNER_MODE = "recent"
                    applied_immediately.append(key)
                elif key == "BANNER_POOL_SIZE":
                    runtime.BANNER_POOL_SIZE = 200
                    applied_immediately.append(key)
                elif key == "BANNER_TAGS":
                    runtime.BANNER_TAGS = []
                    applied_immediately.append(key)
                elif key == "SERVER_NAME":
                    runtime.SERVER_NAME = "Stash Media Server"
                    applied_immediately.append(key)
                elif key == "STASH_TIMEOUT":
                    runtime.STASH_TIMEOUT = 30
                    applied_immediately.append(key)
                elif key == "STASH_RETRIES":
                    runtime.STASH_RETRIES = 3
                    applied_immediately.append(key)
                elif key == "STASH_GRAPHQL_PATH":
                    runtime.STASH_GRAPHQL_PATH = "/graphql"
                    applied_immediately.append(key)
                elif key == "STASH_VERIFY_TLS":
                    runtime.STASH_VERIFY_TLS = False
                    applied_immediately.append(key)
                elif key == "ENABLE_FILTERS":
                    runtime.ENABLE_FILTERS = True
                    applied_immediately.append(key)
                elif key == "ENABLE_IMAGE_RESIZE":
                    runtime.ENABLE_IMAGE_RESIZE = True
                    applied_immediately.append(key)
                elif key == "ENABLE_TAG_FILTERS":
                    runtime.ENABLE_TAG_FILTERS = False
                    applied_immediately.append(key)
                elif key == "ENABLE_ALL_TAGS":
                    runtime.ENABLE_ALL_TAGS = False
                    applied_immediately.append(key)
                elif key == "IMAGE_CACHE_MAX_SIZE":
                    runtime.IMAGE_CACHE_MAX_SIZE = 100
                    applied_immediately.append(key)
                elif key == "DEFAULT_PAGE_SIZE":
                    runtime.DEFAULT_PAGE_SIZE = 50
                    applied_immediately.append(key)
                elif key == "MAX_PAGE_SIZE":
                    runtime.MAX_PAGE_SIZE = 200
                    applied_immediately.append(key)
                elif key == "REQUIRE_AUTH_FOR_CONFIG":
                    runtime.REQUIRE_AUTH_FOR_CONFIG = False
                    applied_immediately.append(key)
                elif key == "LOG_LEVEL":
                    runtime.LOG_LEVEL = "INFO"
                    logger.setLevel(logging.INFO)
                    for handler in logger.handlers:
                        handler.setLevel(logging.INFO)
                    applied_immediately.append(key)
                elif key == "BAN_THRESHOLD":
                    runtime.BAN_THRESHOLD = 10
                    applied_immediately.append(key)
                elif key == "BAN_WINDOW_MINUTES":
                    runtime.BAN_WINDOW_MINUTES = 15
                    applied_immediately.append(key)
                elif key == "BANNED_IPS":
                    runtime.BANNED_IPS = set()
                    applied_immediately.append(key)
                elif key in {k for k, *_ in _P5B_KEYS}:
                    if _p5b_apply_default(key):
                        applied_immediately.append(key)

            # Update _config_defined_keys to reflect new state
            for key in updates:
                runtime.config_defined_keys.add(key)
            for key in commented_keys:
                runtime.config_defined_keys.discard(key)

            if applied_immediately:
                logger.info(f"Applied immediately: {', '.join(applied_immediately)}")
            if needs_restart:
                logger.info(f"Requires restart: {', '.join(needs_restart)}")

            return JSONResponse({
                "success": True,
                "applied_immediately": applied_immediately,
                "needs_restart": needs_restart
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


