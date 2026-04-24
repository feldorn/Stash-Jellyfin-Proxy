"""Starlette application factory.

Builds the proxy `app` and UI `ui_app` Starlette instances from the
route tables and middleware stack. Import these objects to run the server.

`SuppressDisconnectFilter` is also housed here — it filters Hypercorn's
expected disconnect noise before it reaches the log stream.
"""
import asyncio
import logging

from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket  # noqa: F401 (kept for type hints)

from stash_jellyfin_proxy.errors import ERROR_CONTRACT_HANDLERS
from stash_jellyfin_proxy.middleware.auth import AuthenticationMiddleware
from stash_jellyfin_proxy.middleware.logging import RequestLoggingMiddleware
from stash_jellyfin_proxy.middleware.paths import CaseInsensitivePathMiddleware

# --- Endpoint imports ---
from stash_jellyfin_proxy.endpoints.images import endpoint_image
from stash_jellyfin_proxy.endpoints.items import endpoint_items, endpoint_item_details
from stash_jellyfin_proxy.endpoints.misc import endpoint_display_preferences, endpoint_websocket
from stash_jellyfin_proxy.endpoints.playback import endpoint_playback_info
from stash_jellyfin_proxy.endpoints.search import (
    endpoint_items_counts,
    endpoint_items_filters,
    endpoint_genres,
    endpoint_persons,
    endpoint_studios,
    endpoint_search_hints,
)
from stash_jellyfin_proxy.endpoints.stream import endpoint_stream, endpoint_download, endpoint_subtitle
from stash_jellyfin_proxy.endpoints.stubs import (
    endpoint_ping, endpoint_sessions_capabilities, endpoint_sessions_list,
    endpoint_system_endpoint, endpoint_system_info_storage,
    endpoint_scheduled_tasks, endpoint_web_configuration_pages,
    endpoint_activity_log, endpoint_server_domains,
    endpoint_users_list, endpoint_users_public,
    endpoint_branding, endpoint_splashscreen,
    endpoint_quickconnect_enabled, endpoint_quickconnect_stub,
    endpoint_grouping_options,
    endpoint_similar, endpoint_recommendations, endpoint_instant_mix,
    endpoint_intros, endpoint_special_features, endpoint_local_trailers,
    endpoint_theme_songs, endpoint_theme_videos, endpoint_theme_media,
    endpoint_additional_parts, endpoint_ancestors,
    endpoint_user_item_rating,
    endpoint_collections, endpoint_playlists,
    endpoint_artists, endpoint_years,
    endpoint_bitrate_test,
    endpoint_media_segments, endpoint_danmu, endpoint_client_log,
    endpoint_favicon,
    catch_all,
)
from stash_jellyfin_proxy.endpoints.system import endpoint_root, endpoint_system_info, endpoint_public_info
from stash_jellyfin_proxy.endpoints.user_actions import (
    endpoint_user_favorites,
    endpoint_user_item_favorite,
    endpoint_user_item_unfavorite,
    endpoint_user_played_items,
    endpoint_user_unplayed_items,
)
from stash_jellyfin_proxy.endpoints.users import (
    endpoint_authenticate_by_name,
    endpoint_users,
    endpoint_user_by_id,
    endpoint_user_me,
    endpoint_user_image,
)
from stash_jellyfin_proxy.endpoints.views import (
    endpoint_user_views,
    endpoint_virtual_folders,
    endpoint_shows_episodes,
    endpoint_shows_nextup,
    endpoint_shows_seasons,
    endpoint_latest_items,
    endpoint_user_items_resume,
    endpoint_sessions,
)
from stash_jellyfin_proxy.ui.api import (
    ui_index,
    ui_api_status,
    ui_api_logs,
    ui_api_streams,
    ui_api_stats,
    ui_api_stats_reset,
    ui_api_restart,
    ui_api_auth_config,
    ui_api_config,
)

# --- Proxy routes ---
routes = [
    Route("/", endpoint_root),
    Route("/System/Info", endpoint_system_info),
    Route("/System/Info/Public", endpoint_public_info),
    Route("/System/Ping", endpoint_ping),
    Route("/Branding/Configuration", endpoint_branding),
    Route("/Branding/Splashscreen", endpoint_splashscreen),
    Route("/QuickConnect/Enabled", endpoint_quickconnect_enabled),
    Route("/QuickConnect/Initiate", endpoint_quickconnect_stub, methods=["POST", "GET"]),
    Route("/QuickConnect/Connect", endpoint_quickconnect_stub, methods=["POST", "GET"]),
    Route("/Users/AuthenticateByName", endpoint_authenticate_by_name, methods=["POST", "GET"]),
    Route("/Users/Public", endpoint_users_public),
    Route("/UserImage", endpoint_user_image),
    Route("/Users/Me", endpoint_user_me),
    Route("/UserViews", endpoint_user_views),
    Route("/UserViews/GroupingOptions", endpoint_grouping_options),
    Route("/UserItems/Resume", endpoint_user_items_resume),
    Route("/UserItems/Latest", endpoint_latest_items),
    Route("/Users/{user_id}", endpoint_user_by_id),
    Route("/Users/{user_id}/Views", endpoint_user_views),
    Route("/Users/{user_id}/Items/Latest", endpoint_latest_items),
    Route("/Users/{user_id}/Items/Resume", endpoint_user_items_resume),
    Route("/Users/{user_id}/GroupingOptions", endpoint_grouping_options),
    Route("/Users/{user_id}/FavoriteItems", endpoint_user_favorites),
    Route("/Users/{user_id}/Items/{item_id}/LocalTrailers", endpoint_local_trailers),
    Route("/Users/{user_id}/Items/{item_id}/Rating", endpoint_user_item_rating, methods=["POST", "DELETE"]),
    Route("/Users/{user_id}/FavoriteItems/{item_id}", endpoint_user_item_favorite, methods=["POST"]),
    Route("/Users/{user_id}/FavoriteItems/{item_id}", endpoint_user_item_unfavorite, methods=["DELETE"]),
    Route("/Users/{user_id}/FavoriteItems/{item_id}/Delete", endpoint_user_item_unfavorite, methods=["POST", "DELETE"]),
    Route("/UserFavoriteItems/{item_id}", endpoint_user_item_favorite, methods=["POST"]),
    Route("/UserFavoriteItems/{item_id}", endpoint_user_item_unfavorite, methods=["DELETE"]),
    Route("/UserFavoriteItems/{item_id}/Delete", endpoint_user_item_unfavorite, methods=["POST", "DELETE"]),
    Route("/Users/{user_id}/PlayedItems/{item_id}", endpoint_user_played_items, methods=["POST"]),
    Route("/Users/{user_id}/PlayingItems/{item_id}", endpoint_user_played_items, methods=["POST", "DELETE"]),
    Route("/Users/{user_id}/UnplayedItems/{item_id}", endpoint_user_unplayed_items, methods=["POST", "DELETE"]),
    Route("/Library/VirtualFolders", endpoint_virtual_folders),
    Route("/DisplayPreferences/{prefs_id}", endpoint_display_preferences, methods=["GET", "POST"]),
    Route("/Shows/NextUp", endpoint_shows_nextup),
    Route("/Shows/{series_id}/Seasons", endpoint_shows_seasons),
    Route("/Shows/{series_id}/Episodes", endpoint_shows_episodes),
    Route("/Users/{user_id}/Items", endpoint_items),
    Route("/Users/{user_id}/Items/{item_id}", endpoint_item_details),
    Route("/Items", endpoint_items),
    Route("/Items/Counts", endpoint_items_counts),
    Route("/Items/Latest", endpoint_latest_items),
    Route("/Items/Filters", endpoint_items_filters),
    Route("/Items/{item_id}/Download", endpoint_download),
    Route("/Items/{item_id}/PlaybackInfo", endpoint_playback_info, methods=["GET", "POST"]),
    Route("/Items/{item_id}/Similar", endpoint_similar),
    Route("/Items/{item_id}/Intros", endpoint_intros),
    Route("/Users/{user_id}/Items/{item_id}/Intros", endpoint_intros),
    Route("/Items/{item_id}/SpecialFeatures", endpoint_special_features),
    Route("/Items/{item_id}/LocalTrailers", endpoint_local_trailers),
    Route("/Users/{user_id}/Items/{item_id}/SpecialFeatures", endpoint_special_features),
    Route("/Users/{user_id}/Items/{item_id}/LocalTrailers", endpoint_local_trailers),
    Route("/Items/{item_id}/ThemeSongs", endpoint_theme_songs),
    Route("/Items/{item_id}/ThemeVideos", endpoint_theme_videos),
    Route("/Items/{item_id}/ThemeMedia", endpoint_theme_media),
    Route("/Users/{user_id}/Items/{item_id}/ThemeSongs", endpoint_theme_songs),
    Route("/Users/{user_id}/Items/{item_id}/ThemeVideos", endpoint_theme_videos),
    Route("/Users/{user_id}/Items/{item_id}/ThemeMedia", endpoint_theme_media),
    Route("/Videos/{item_id}/AdditionalParts", endpoint_additional_parts),
    Route("/Items/{item_id}/Ancestors", endpoint_ancestors),
    Route("/Users/{user_id}/Items/{item_id}/Ancestors", endpoint_ancestors),
    Route("/System/Endpoint", endpoint_system_endpoint),
    Route("/Users", endpoint_users_list),
    Route("/Sessions", endpoint_sessions_list),
    Route("/System/Info/Storage", endpoint_system_info_storage),
    Route("/ScheduledTasks", endpoint_scheduled_tasks),
    Route("/web/ConfigurationPages", endpoint_web_configuration_pages),
    Route("/System/ActivityLog/Entries", endpoint_activity_log),
    Route("/System/Ext/ServerDomains", endpoint_server_domains),
    Route("/favicon.ico", endpoint_favicon),
    Route("/Users/{user_id}/Images/Primary", endpoint_user_image),
    Route("/Playback/BitrateTest", endpoint_bitrate_test),
    Route("/Videos/{item_id}/stream", endpoint_stream),
    Route("/Videos/{item_id}/Stream", endpoint_stream),
    Route("/Videos/{item_id}/stream.{ext}", endpoint_stream),
    Route("/Videos/{item_id}/Stream.{ext}", endpoint_stream),
    Route("/videos/{item_id}/stream", endpoint_stream),
    Route("/videos/{item_id}/stream.{ext}", endpoint_stream),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/Stream.vtt", endpoint_subtitle),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/0/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/0/Stream.vtt", endpoint_subtitle),
    Route("/Videos/{item_id}/{item_id2}/Subtitles/{subtitle_index}/0/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/{item_id2}/Subtitles/{subtitle_index}/0/Stream.vtt", endpoint_subtitle),
    Route("/Videos/{item_id}/{item_id2}/Subtitles/{subtitle_index}/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/{item_id2}/Subtitles/{subtitle_index}/Stream.vtt", endpoint_subtitle),
    Route("/Items/{item_id}", endpoint_item_details),
    Route("/Items/{item_id}/Images/Primary", endpoint_image),
    Route("/Items/{item_id}/Images/Thumb", endpoint_image),
    Route("/Items/{item_id}/Images/Backdrop", endpoint_image),
    Route("/Items/{item_id}/Images/Backdrop/{index}", endpoint_image),
    Route("/PlaybackInfo", endpoint_playback_info, methods=["POST", "GET"]),
    Route("/Sessions/Playing", endpoint_sessions, methods=["POST"]),
    Route("/Sessions/Playing/Progress", endpoint_sessions, methods=["POST"]),
    Route("/Sessions/Playing/Stopped", endpoint_sessions, methods=["POST"]),
    Route("/Sessions/Capabilities", endpoint_sessions_capabilities, methods=["POST"]),
    Route("/Sessions/Capabilities/Full", endpoint_sessions_capabilities, methods=["POST"]),
    Route("/ClientLog/Document", endpoint_client_log, methods=["POST"]),
    Route("/Collections", endpoint_collections),
    Route("/Playlists", endpoint_playlists),
    Route("/Genres", endpoint_genres),
    Route("/MusicGenres", endpoint_genres),
    Route("/Persons", endpoint_persons),
    Route("/Studios", endpoint_studios),
    Route("/Artists", endpoint_artists),
    Route("/Years", endpoint_years),
    Route("/Search/Hints", endpoint_search_hints),
    Route("/Movies/Recommendations", endpoint_recommendations),
    Route("/Items/{item_id}/InstantMix", endpoint_instant_mix),
    Route("/MediaSegments/{item_id}", endpoint_media_segments),
    Route("/api/danmu/{item_id}/raw", endpoint_danmu),
    WebSocketRoute("/socket", endpoint_websocket),
    WebSocketRoute("/{path:path}", endpoint_websocket),
    Route("/{path:path}", catch_all),
]

CaseInsensitivePathMiddleware.build_path_map(routes)

# CORS above auth: browsers send OPTIONS preflights without Authorization;
# auth would 401 them and strip CORS headers before the browser sees them.
# allow_private_network=True opts into Chrome's Private Network Access spec.
middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_private_network=True),
    Middleware(RequestLoggingMiddleware),
    Middleware(CaseInsensitivePathMiddleware),
    Middleware(AuthenticationMiddleware),
]

# debug=False so Starlette's HTML debug page never leaks tracebacks.
# _unhandled_exception_handler below returns JSON 500 instead.
# NOTE: do NOT register a catch-all Exception handler here. That catches
# ConnectionReset / asyncio.CancelledError during streaming responses
# before they reach RequestLoggingMiddleware's try/except, silently
# breaking stream-stop logging and dashboard active-stream tracking.
app = Starlette(
    debug=False,
    routes=routes,
    middleware=middleware,
    exception_handlers=ERROR_CONTRACT_HANDLERS,
)

# --- Web UI app ---
_UI_STATIC_DIR = Path(__file__).parent / "ui" / "static"
ui_routes = [
    Route("/", ui_index),
    # app.css + app.js extracted from the embedded HTML (Phase 5A).
    Mount("/static", app=StaticFiles(directory=str(_UI_STATIC_DIR)), name="ui-static"),
    Route("/api/status", ui_api_status),
    Route("/api/config", ui_api_config, methods=["GET", "POST"]),
    Route("/api/auth-config", ui_api_auth_config, methods=["POST"]),
    Route("/api/logs", ui_api_logs),
    Route("/api/streams", ui_api_streams),
    Route("/api/stats", ui_api_stats),
    Route("/api/stats/reset", ui_api_stats_reset, methods=["POST"]),
    Route("/api/restart", ui_api_restart, methods=["POST"]),
]

ui_middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_private_network=True),
]

ui_app = Starlette(debug=False, routes=ui_routes, middleware=ui_middleware)


class SuppressDisconnectFilter(logging.Filter):
    """Filter expected socket-disconnect noise from Hypercorn's error logger."""

    def filter(self, record):
        msg = record.getMessage()
        if "socket.send() raised exception" in msg:
            return False
        if "socket.recv() raised exception" in msg:
            return False
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type in (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                return False
        return True
