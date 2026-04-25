"""Endpoints that real Jellyfin clients poll but that Stash has no real
data to back. Each one returns a typed, shape-correct empty response so
strict-schema clients parse cleanly and logs stay quiet.

These were inventoried from live Infuse, Swiftfin, SenPlayer, and
jellyfin-web sessions.
"""
import logging
import os

from starlette.responses import JSONResponse, Response

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.user import build_user_dto
from stash_jellyfin_proxy.stash.client import _get_async_client

logger = logging.getLogger("stash-jellyfin-proxy")


# --- Health / identity probes ---

async def endpoint_ping(request):
    """`GET /System/Ping` — Emby compatibility."""
    return Response(content="Emby Server", media_type="text/plain")


async def endpoint_sessions_capabilities(request):
    """`POST /Sessions/Capabilities[/Full]` — client capability registration.
    We don't negotiate capabilities; 204 is the accepted no-op response."""
    return Response(status_code=204)


async def endpoint_sessions_list(request):
    """`GET /Sessions` — active-sessions list. SenPlayer polls this.
    Empty list renders as 'no other clients playing'."""
    return JSONResponse([])


async def endpoint_system_endpoint(request):
    """`GET /System/Endpoint` — endpoint info. Shape: EndpointInfo."""
    return JSONResponse({"IsLocal": True, "IsInNetwork": True})


async def endpoint_system_info_storage(request):
    """`GET /System/Info/Storage` — disk usage report. Zeroed stub;
    the proxy doesn't own Stash's storage."""
    return JSONResponse({
        "ProgramDataFolder": {"Name": "Program Data", "Path": "/config", "FreeSpace": 0, "UsedSpace": 0, "StorageType": "Unknown"},
        "LogFolder": {"Name": "Logs", "Path": runtime.LOG_DIR, "FreeSpace": 0, "UsedSpace": 0, "StorageType": "Unknown"},
        "CacheFolder": {"Name": "Cache", "Path": "/tmp", "FreeSpace": 0, "UsedSpace": 0, "StorageType": "Unknown"},
        "InternalMetadataFolders": [],
        "ExternalMetadataFolders": [],
        "LibraryFolders": [],
    })


async def endpoint_scheduled_tasks(request):
    """`GET /ScheduledTasks` — Jellyfin's task scheduler list."""
    return JSONResponse([])


async def endpoint_web_configuration_pages(request):
    """`GET /web/ConfigurationPages` — Jellyfin Web admin tab hooks."""
    return JSONResponse([])


async def endpoint_activity_log(request):
    """`GET /System/ActivityLog/Entries` — paginated activity feed."""
    start = int(request.query_params.get("startIndex", "0"))
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start})


async def endpoint_server_domains(request):
    """`GET /System/Ext/ServerDomains` — SenPlayer-specific domain list."""
    return JSONResponse([])


# --- Users list (alias to /Users/Public) ---

async def endpoint_users_list(request):
    """`GET /Users` — configured users. Single-user stash_jellyfin_proxy."""
    return JSONResponse([build_user_dto()])


async def endpoint_users_public(request):
    """`GET /Users/Public` — public users for the login screen.
    Strongly-typed clients reject responses missing Policy/Configuration;
    build_user_dto returns the full schema."""
    return JSONResponse([build_user_dto()])


# --- Branding / QuickConnect (not configured) ---

async def endpoint_branding(request):
    """`GET /Branding/Configuration` — no custom branding."""
    return JSONResponse({
        "LoginDisclaimer": None,
        "CustomCss": None,
        "SplashscreenEnabled": False,
    })


async def endpoint_splashscreen(request):
    """`GET /Branding/Splashscreen` — none configured."""
    return Response(status_code=404)


async def endpoint_quickconnect_enabled(request):
    return Response(content="false", media_type="application/json")


async def endpoint_quickconnect_stub(request):
    return JSONResponse({"ErrCode": "QuickConnect not enabled"}, status_code=400)


# --- Grouping / folder metadata ---

async def endpoint_grouping_options(request):
    """`GET /UserViews/GroupingOptions` — collection-type options."""
    return JSONResponse([])


# --- Item-detail companion stubs ---

async def endpoint_similar(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_recommendations(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_instant_mix(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_intros(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_special_features(request):
    """Jellyfin spec returns a plain array for special features."""
    return JSONResponse([])


async def endpoint_local_trailers(request):
    """Stash doesn't model trailers — always empty."""
    return JSONResponse([])


async def endpoint_theme_songs(request):
    """ThemeSongs stub. Response MUST include OwnerId — the web client's
    ThemeMediaPlayer reads `result.OwnerId` unconditionally and crashes
    with `Cannot read properties of undefined (reading 'OwnerId')` without it."""
    return JSONResponse({
        "Items": [],
        "TotalRecordCount": 0,
        "OwnerId": request.path_params.get("item_id", ""),
    })


async def endpoint_theme_videos(request):
    return JSONResponse({
        "Items": [],
        "TotalRecordCount": 0,
        "OwnerId": request.path_params.get("item_id", ""),
    })


async def endpoint_theme_media(request):
    """Combined ThemeSongs + ThemeVideos (AllThemeMediaResult)."""
    iid = request.path_params.get("item_id", "")
    empty = {"Items": [], "TotalRecordCount": 0, "OwnerId": iid}
    return JSONResponse({
        "ThemeSongsResult": empty,
        "ThemeVideosResult": empty,
        "SoundtrackIds": [],
    })


async def endpoint_additional_parts(request):
    return JSONResponse({
        "Items": [],
        "TotalRecordCount": 0,
        "OwnerId": request.path_params.get("item_id", ""),
    })


async def endpoint_ancestors(request):
    """Jellyfin returns a plain BaseItemDto[] array."""
    return JSONResponse([])


# --- User-action stubs ---

async def endpoint_user_item_rating(request):
    """Rating update — accepts the POST but doesn't persist. Stash
    uses rating100 with a different scale that isn't currently mapped."""
    return JSONResponse({})


# --- Collections / taxonomic lists we don't expose ---

async def endpoint_collections(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_media_folders(request):
    """`GET /Library/MediaFolders` — admin-side library list. We expose
    libraries via `/Library/VirtualFolders` already; this is the older
    alias the JF web client probes."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_livetv_channels(request):
    """`GET /LiveTv/Channels` — Live TV catalog. We don't model Live TV."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_artists(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_years(request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


# --- Bandwidth probe ---

async def endpoint_bitrate_test(request):
    """`GET /Playback/BitrateTest?Size=N` — return N random bytes.
    Web client sends `Size=` (capital S); accept either casing."""
    qp = request.query_params
    size = int(qp.get("Size") or qp.get("size") or 1000000)
    size = min(size, 10000000)
    return Response(content=os.urandom(size), media_type="application/octet-stream")


# --- Client-specific stubs ---

async def endpoint_media_segments(request):
    """MediaSegments API — Infuse doesn't use it; stub prevents unhandled
    warnings on clients that probe for intro/outro/chapter markers."""
    return JSONResponse({"Items": []})


async def endpoint_danmu(request):
    """SenPlayer danmaku (bullet comments) endpoint."""
    return JSONResponse([])


async def endpoint_client_log(request):
    """Jellyfin Android's startup diagnostic log dump."""
    return Response(status_code=204)


# --- Favicon (proxied from Stash) ---

_favicon_cache = None


async def endpoint_favicon(request):
    """Serve Stash's favicon, proxied once and cached in-memory for the
    lifetime of the process. Falls back to a minimal SVG if Stash is
    unreachable."""
    global _favicon_cache
    if _favicon_cache is None:
        try:
            client = _get_async_client()
            resp = await client.get(f"{runtime.STASH_URL.rstrip('/')}/favicon.ico", timeout=5)
            resp.raise_for_status()
            _favicon_cache = (resp.content, resp.headers.get("content-type", "image/vnd.microsoft.icon"))
        except Exception as e:
            logger.warning(f"favicon fetch from Stash failed: {e}; serving SVG fallback")
            svg = (
                b'<?xml version="1.0"?>'
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                b'<rect width="32" height="32" fill="#1a1a2e"/>'
                b'<text x="16" y="23" font-family="Arial,sans-serif" font-size="22" '
                b'font-weight="bold" fill="#4a90d9" text-anchor="middle">S</text>'
                b'</svg>'
            )
            _favicon_cache = (svg, "image/svg+xml")
    data, ct = _favicon_cache
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --- Catch-all ---

async def catch_all(request):
    """Log unhandled routes and return an empty-paginated fallback."""
    path = request.url.path
    # JF web client occasionally renders an item card before its Id resolves
    # and emits `/Items//` or `/Users/<id>/Items//`. The double-slash isn't
    # routable; treat it as a quiet 400 instead of a noisy UNHANDLED warning.
    if "/Items//" in path:
        logger.debug(f"Empty item id: {request.method} {path}")
        return JSONResponse({"error": "Empty item id"}, status_code=400)
    logger.warning(f"UNHANDLED ENDPOINT: {request.method} {path} - Query: {dict(request.query_params)}")
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
