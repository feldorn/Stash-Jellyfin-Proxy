"""Web UI handlers — the shell HTML and the /api/* JSON endpoints the
dashboard polls.

Most handlers read or mutate state that already lives in proxy.runtime
or proxy.state. The one remaining holdout is ui_api_config, which still
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

from proxy import runtime
from proxy.stash.client import check_stash_connection_cached, stash_query
from proxy.state import stats as _stats
from proxy.state import streams as _streams

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
        result = stash_query(query, {})
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
