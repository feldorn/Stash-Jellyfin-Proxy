#!/usr/bin/env python3
"""
Stash-Jellyfin Proxy v6.02
Enables Infuse and other Jellyfin clients to connect to Stash by emulating the Jellyfin API.

# =============================================================================
# TODO / KNOWN ISSUES
# =============================================================================
#
# Dashboard Freezing During Stream Start
# --------------------------------------
# The Web UI dashboard can briefly freeze when Infuse starts a new stream.
# Cause: Synchronous Stash API calls block the async event loop during metadata
#        and image fetching, delaying UI polling requests.
# Possible fixes:
#   - Replace `requests` with async `httpx` client
#   - Cache Stash connection status in background instead of live checks
#   - Run Stash queries in thread pool via asyncio.to_thread()
#
# Infuse Image Caching
# --------------------
# Infuse aggressively caches images and may not refresh when Stash artwork changes.
# This is Infuse behavior, not a proxy issue. Users can clear Infuse metadata cache.
#
# =============================================================================
"""
import os
import sys
import json
import logging
import asyncio
import signal
import uuid
import hashlib
import argparse
import time
import random
import re
import datetime
from urllib.parse import parse_qs
from typing import Optional, List, Dict, Any, Tuple
from logging.handlers import SysLogHandler, RotatingFileHandler

# Force UTF-8 on Windows consoles (cp1252 would crash on emoji log messages).
# Must run before any print() or logger output.
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass

# Early CLI pre-scan: --config and --local-config need to land in env vars
# before the module-level config load runs. The full argparse (with --help,
# --debug, etc.) still happens later in main().
def _prescan_config_args(argv):
    """Consume --config and --local-config from argv and promote to env vars."""
    for flag, env_var in (("--config", "CONFIG_FILE"), ("--local-config", "LOCAL_CONFIG_FILE")):
        for i, arg in enumerate(argv):
            if arg == flag and i + 1 < len(argv):
                os.environ[env_var] = argv[i + 1]
                break
            if arg.startswith(flag + "="):
                os.environ[env_var] = arg.split("=", 1)[1]
                break

_prescan_config_args(sys.argv[1:])

# Third-party dependencies
try:
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response, RedirectResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.cors import CORSMiddleware
    import requests
except ImportError as e:
    print(f"Missing dependency: {e}. Please run: pip install hypercorn starlette requests")
    sys.exit(1)

# Optional Pillow for image resizing (graceful fallback if not installed)
try:
    from PIL import Image
    import io
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    print("Note: Pillow not installed. Studio images will not be resized. Install with: pip install Pillow")

# Optional setproctitle so `ps` / `top` / `pgrep` show "stash-jellyfin-proxy"
# instead of a bare "python". Not required — skip silently if unavailable.
try:
    import setproctitle
    setproctitle.setproctitle("stash-jellyfin-proxy")
except ImportError:
    pass

# Image helpers live in proxy/util/images.py. PLACEHOLDER_PNG is also
# exposed for any remaining monolith code that still references it by
# name (removed once all consumers are extracted).
from proxy.util.images import (  # noqa: F401
    pad_image_to_portrait,
    generate_text_icon,
    generate_menu_icon,
    generate_filter_icon,
    generate_placeholder_icon,
    placeholder_png as _placeholder_png,
)
PLACEHOLDER_PNG = _placeholder_png()

# --- Configuration Loading ---
# Config file location: same directory as script, or specified path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.join(SCRIPT_DIR, "stash_jellyfin_proxy.conf"))

# Default Configuration (can be overridden by config file)
STASH_URL = "https://stash:9999"
STASH_API_KEY = ""  # Real Stash API key from Settings -> Security -> API Key
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
UI_PORT = 8097  # Web UI port (set to 0 to disable)
# User credentials for Infuse authentication (must be set in config)
SJS_USER = ""
SJS_PASSWORD = ""

# Tag groups - comma-separated list of tag names to show as top-level folders
TAG_GROUPS = []  # e.g., ["Favorites", "VR", "4K"]

# Favorite tag - Stash tag name used for favorites (toggled from Infuse/Swiftfin)
FAVORITE_TAG = ""  # e.g., "Favorite"

# Latest groups - controls which libraries show "Latest" on home page
# Empty = show all libraries, or list specific ones: "Scenes, VR, Favorites"
LATEST_GROUPS = []

# Banner (home-screen hero) — some clients (SenPlayer) request Movie-only items
# with SortBy=...Random... for the rotating banner on the server's home screen.
# When that signature is detected, return Scenes (with screenshots) instead of Groups.
# BANNER_MODE: "recent" = random sample from newest BANNER_POOL_SIZE scenes
#              "tag"    = random sample from scenes matching any BANNER_TAGS
BANNER_MODE = "recent"
BANNER_POOL_SIZE = 200
BANNER_TAGS = []  # e.g., ["Featured", "Showcase"]

# Server identity
SERVER_NAME = "Stash Media Server"
SERVER_ID = ""  # Required - must be set in config file
# Jellyfin server version we advertise. Newer Android/Findroid/Afinity clients
# gate on a minimum version. Overridable in config for client-compat testing.
JELLYFIN_VERSION = "10.11.0"

# Pagination settings
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Feature toggles
ENABLE_FILTERS = True
ENABLE_IMAGE_RESIZE = True
ENABLE_TAG_FILTERS = False  # Show Tags folder with tag-based navigation
ENABLE_ALL_TAGS = False  # Show "All Tags" subfolder (can be large)
REQUIRE_AUTH_FOR_CONFIG = False

# Performance settings
STASH_TIMEOUT = 30
STASH_RETRIES = 3

# GraphQL endpoint path (use /graphql-local for SWAG reverse proxy bypass)
STASH_GRAPHQL_PATH = "/graphql"

# TLS verification (set to false for self-signed certs in Docker)
STASH_VERIFY_TLS = False

# Logging settings
LOG_DIR = "."  # Current directory
LOG_FILE = "stash_jellyfin_proxy.log"
LOG_LEVEL = "INFO"
LOG_MAX_SIZE_MB = 10
LOG_BACKUP_COUNT = 3

# Image cache settings
IMAGE_CACHE_MAX_SIZE = 100  # Max items to cache

# IP Ban settings
BANNED_IPS = set()  # Set of banned IP addresses
BAN_THRESHOLD = 10  # Failed attempts before ban
BAN_WINDOW_MINUTES = 15  # Rolling window for counting failures

# Config loader lives in proxy/config/loader.py (Phase 0.6 leaf).
from proxy.config.loader import load_config

# Pure config helpers live in proxy/config/helpers.py (Phase 0.6 leaf).
from proxy.config.helpers import (  # noqa: F401
    parse_bool,
    normalize_path,
    normalize_server_id,
    generate_server_id,
    save_config_value,
    save_server_id_to_config,
)

def _default_local_config_path(base_path):
    """Derive a sibling `.local` override path from the base config path.
    e.g. stash_jellyfin_proxy.conf -> stash_jellyfin_proxy.local.conf"""
    root, ext = os.path.splitext(base_path)
    return f"{root}.local{ext}" if ext else f"{base_path}.local"

LOCAL_CONFIG_FILE = os.getenv("LOCAL_CONFIG_FILE", _default_local_config_path(CONFIG_FILE))

_config, _config_defined_keys, _config_sections = load_config(CONFIG_FILE)

# --- v1 → v2 config migration -------------------------------------------
# Logic lives in proxy/config/migration.py. Module-level state set here
# from the return value (MIGRATION_PERFORMED consumed by the Web UI to
# show the one-time migration banner).
from proxy.config.migration import (
    run_config_migration,
    CURRENT_CONFIG_VERSION,
)

MIGRATION_PERFORMED = False
MIGRATION_LOG = []

# Run migration against the primary config. Local-override file is left
# untouched (it's per-user, merged later, and may intentionally hold
# sparse overrides).
_config, _config_sections, MIGRATION_PERFORMED, MIGRATION_LOG = run_config_migration(
    CONFIG_FILE, _config, _config_defined_keys, _config_sections
)
if MIGRATION_PERFORMED:
    print(f"Config migrated to v{CURRENT_CONFIG_VERSION}:")
    for line in MIGRATION_LOG:
        print(f"  [migrate] {line}")
# Rebuild defined_keys from the post-migration state so later accessors
# still see the right key set.
_config_defined_keys = set(_config.keys())

# Merge local override on top (per-user edits stay out of the shipped conf).
if os.path.isfile(LOCAL_CONFIG_FILE) and os.path.abspath(LOCAL_CONFIG_FILE) != os.path.abspath(CONFIG_FILE):
    _local_config, _local_defined_keys, _local_sections = load_config(LOCAL_CONFIG_FILE)
    if _local_config or _local_sections:
        _config.update(_local_config)
        _config_defined_keys.update(_local_defined_keys)
        # Section merge is key-level: local wins per-key inside each section,
        # and a section only present locally is added whole.
        for section_name, section_body in _local_sections.items():
            _config_sections.setdefault(section_name, {}).update(section_body)
        print(f"Loaded local override from {LOCAL_CONFIG_FILE}")


if _config:
    STASH_URL = _config.get("STASH_URL", STASH_URL)
    STASH_API_KEY = _config.get("STASH_API_KEY", STASH_API_KEY)
    PROXY_BIND = _config.get("PROXY_BIND", PROXY_BIND)
    PROXY_PORT = int(_config.get("PROXY_PORT", PROXY_PORT))
    if "UI_PORT" in _config:
        UI_PORT = int(_config.get("UI_PORT", UI_PORT))
    SJS_USER = _config.get("SJS_USER", SJS_USER)
    SJS_PASSWORD = _config.get("SJS_PASSWORD", SJS_PASSWORD)
    # Parse TAG_GROUPS as comma-separated list
    tag_groups_str = _config.get("TAG_GROUPS", "")
    if tag_groups_str:
        TAG_GROUPS = [t.strip() for t in tag_groups_str.split(",") if t.strip()]
    # Favorite tag
    FAVORITE_TAG = _config.get("FAVORITE_TAG", FAVORITE_TAG).strip()
    # Parse LATEST_GROUPS as comma-separated list
    latest_groups_str = _config.get("LATEST_GROUPS", "")
    if latest_groups_str:
        LATEST_GROUPS = [t.strip() for t in latest_groups_str.split(",") if t.strip()]
    # Banner settings
    if "BANNER_MODE" in _config:
        mode = _config.get("BANNER_MODE", BANNER_MODE).strip().lower()
        BANNER_MODE = mode if mode in ("recent", "tag") else "recent"
    if "BANNER_POOL_SIZE" in _config:
        try:
            BANNER_POOL_SIZE = max(1, int(_config.get("BANNER_POOL_SIZE", BANNER_POOL_SIZE)))
        except ValueError:
            pass
    banner_tags_str = _config.get("BANNER_TAGS", "")
    if banner_tags_str:
        BANNER_TAGS = [t.strip() for t in banner_tags_str.split(",") if t.strip()]

    # Server identity
    SERVER_NAME = _config.get("SERVER_NAME", SERVER_NAME)
    SERVER_ID = _config.get("SERVER_ID", SERVER_ID)
    JELLYFIN_VERSION = _config.get("JELLYFIN_VERSION", JELLYFIN_VERSION).strip() or JELLYFIN_VERSION

    # Pagination settings
    if "DEFAULT_PAGE_SIZE" in _config:
        DEFAULT_PAGE_SIZE = int(_config.get("DEFAULT_PAGE_SIZE", DEFAULT_PAGE_SIZE))
    if "MAX_PAGE_SIZE" in _config:
        MAX_PAGE_SIZE = int(_config.get("MAX_PAGE_SIZE", MAX_PAGE_SIZE))

    # Feature toggles
    if "ENABLE_FILTERS" in _config:
        ENABLE_FILTERS = parse_bool(_config.get("ENABLE_FILTERS"), ENABLE_FILTERS)
    if "ENABLE_IMAGE_RESIZE" in _config:
        ENABLE_IMAGE_RESIZE = parse_bool(_config.get("ENABLE_IMAGE_RESIZE"), ENABLE_IMAGE_RESIZE)
    if "ENABLE_TAG_FILTERS" in _config:
        ENABLE_TAG_FILTERS = parse_bool(_config.get("ENABLE_TAG_FILTERS"), ENABLE_TAG_FILTERS)
    if "ENABLE_ALL_TAGS" in _config:
        ENABLE_ALL_TAGS = parse_bool(_config.get("ENABLE_ALL_TAGS"), ENABLE_ALL_TAGS)
    if "REQUIRE_AUTH_FOR_CONFIG" in _config:
        REQUIRE_AUTH_FOR_CONFIG = parse_bool(_config.get("REQUIRE_AUTH_FOR_CONFIG"), REQUIRE_AUTH_FOR_CONFIG)
    if "IMAGE_CACHE_MAX_SIZE" in _config:
        IMAGE_CACHE_MAX_SIZE = int(_config.get("IMAGE_CACHE_MAX_SIZE", 100))

    # Performance settings
    if "STASH_TIMEOUT" in _config:
        STASH_TIMEOUT = int(_config.get("STASH_TIMEOUT", STASH_TIMEOUT))
    if "STASH_RETRIES" in _config:
        STASH_RETRIES = int(_config.get("STASH_RETRIES", STASH_RETRIES))

    # GraphQL endpoint settings
    if "STASH_GRAPHQL_PATH" in _config:
        STASH_GRAPHQL_PATH = normalize_path(_config.get("STASH_GRAPHQL_PATH", STASH_GRAPHQL_PATH))
    if "STASH_VERIFY_TLS" in _config:
        STASH_VERIFY_TLS = parse_bool(_config.get("STASH_VERIFY_TLS"), STASH_VERIFY_TLS)

    # Logging settings
    if "LOG_DIR" in _config:
        LOG_DIR = _config.get("LOG_DIR", LOG_DIR)
    if "LOG_FILE" in _config:
        LOG_FILE = _config.get("LOG_FILE", LOG_FILE)
    if "LOG_LEVEL" in _config:
        LOG_LEVEL = _config.get("LOG_LEVEL", LOG_LEVEL).upper()
    if "LOG_MAX_SIZE_MB" in _config:
        LOG_MAX_SIZE_MB = int(_config.get("LOG_MAX_SIZE_MB", LOG_MAX_SIZE_MB))
    if "LOG_BACKUP_COUNT" in _config:
        LOG_BACKUP_COUNT = int(_config.get("LOG_BACKUP_COUNT", LOG_BACKUP_COUNT))

    # IP Ban settings
    if "BANNED_IPS" in _config:
        banned_str = _config.get("BANNED_IPS", "")
        if banned_str:
            BANNED_IPS = set(ip.strip() for ip in banned_str.split(",") if ip.strip())
    if "BAN_THRESHOLD" in _config:
        BAN_THRESHOLD = int(_config.get("BAN_THRESHOLD", BAN_THRESHOLD))
    if "BAN_WINDOW_MINUTES" in _config:
        BAN_WINDOW_MINUTES = int(_config.get("BAN_WINDOW_MINUTES", BAN_WINDOW_MINUTES))

    print(f"Loaded config from {CONFIG_FILE}")
else:
    _config_defined_keys = set()
    print(f"Warning: Config file {CONFIG_FILE} not found or empty. Using defaults/env vars.")

# Environment variables ALWAYS override config file (for Docker deployment flexibility)
# This allows docker-compose env vars to take precedence over the mounted config file
# Note: Dockerfile sets defaults for PROXY_BIND, PROXY_PORT, UI_PORT, LOG_DIR
# Only mark as "override" if the value differs from Docker defaults (user explicitly set it)
_DOCKER_ENV_DEFAULTS = {
    "PROXY_BIND": "0.0.0.0",
    "PROXY_PORT": "8096",
    "UI_PORT": "8097",
    "LOG_DIR": "/config",
}
_env_overrides = []

if os.getenv("STASH_URL"):
    STASH_URL = os.getenv("STASH_URL")
    _env_overrides.append("STASH_URL")
if os.getenv("STASH_API_KEY"):
    STASH_API_KEY = os.getenv("STASH_API_KEY")
    _env_overrides.append("STASH_API_KEY")
# These have Docker ENV defaults - only mark as override if value differs
if os.getenv("PROXY_BIND"):
    PROXY_BIND = os.getenv("PROXY_BIND")
    if os.getenv("PROXY_BIND") != _DOCKER_ENV_DEFAULTS["PROXY_BIND"]:
        _env_overrides.append("PROXY_BIND")
if os.getenv("PROXY_PORT"):
    PROXY_PORT = int(os.getenv("PROXY_PORT"))
    if os.getenv("PROXY_PORT") != _DOCKER_ENV_DEFAULTS["PROXY_PORT"]:
        _env_overrides.append("PROXY_PORT")
if os.getenv("UI_PORT"):
    UI_PORT = int(os.getenv("UI_PORT"))
    if os.getenv("UI_PORT") != _DOCKER_ENV_DEFAULTS["UI_PORT"]:
        _env_overrides.append("UI_PORT")
if os.getenv("LOG_DIR"):
    LOG_DIR = os.getenv("LOG_DIR")
    if os.getenv("LOG_DIR") != _DOCKER_ENV_DEFAULTS["LOG_DIR"]:
        _env_overrides.append("LOG_DIR")
# Regular env overrides (no Docker defaults)
if os.getenv("SJS_USER"):
    SJS_USER = os.getenv("SJS_USER")
    _env_overrides.append("SJS_USER")
if os.getenv("SJS_PASSWORD"):
    SJS_PASSWORD = os.getenv("SJS_PASSWORD")
    _env_overrides.append("SJS_PASSWORD")
if os.getenv("SERVER_ID"):
    SERVER_ID = os.getenv("SERVER_ID")
    _env_overrides.append("SERVER_ID")
if os.getenv("JELLYFIN_VERSION"):
    JELLYFIN_VERSION = os.getenv("JELLYFIN_VERSION")
    _env_overrides.append("JELLYFIN_VERSION")
if os.getenv("REQUIRE_AUTH_FOR_CONFIG"):
    REQUIRE_AUTH_FOR_CONFIG = os.getenv("REQUIRE_AUTH_FOR_CONFIG", "").lower() in ('true', 'yes', '1', 'on')
    _env_overrides.append("REQUIRE_AUTH_FOR_CONFIG")
if os.getenv("STASH_GRAPHQL_PATH"):
    STASH_GRAPHQL_PATH = normalize_path(os.getenv("STASH_GRAPHQL_PATH"))
    _env_overrides.append("STASH_GRAPHQL_PATH")
if os.getenv("STASH_VERIFY_TLS"):
    STASH_VERIFY_TLS = os.getenv("STASH_VERIFY_TLS", "").lower() in ('true', 'yes', '1', 'on')
    _env_overrides.append("STASH_VERIFY_TLS")

if _env_overrides:
    print(f"  Env overrides: {', '.join(_env_overrides)}")

# Print effective configuration
if SJS_USER and SJS_PASSWORD:
    print(f"  User: {SJS_USER}")
    print(f"  Password: configured ({len(SJS_PASSWORD)} chars)")
else:
    print("WARNING: Login credentials not configured!")
    print("  Set SJS_USER and SJS_PASSWORD in config file or environment.")
    print("  Without credentials, Infuse will not be able to connect.")
print(f"  Stash URL: {STASH_URL}")
print(f"  GraphQL path: {STASH_GRAPHQL_PATH}")
if not STASH_VERIFY_TLS:
    print(f"  TLS verify: disabled")
print(f"  Proxy: {PROXY_BIND}:{PROXY_PORT}")
if STASH_API_KEY:
    print(f"  API key: configured ({len(STASH_API_KEY)} chars)")
else:
    print("WARNING: STASH_API_KEY not set!")
    print("  Images will not load. Set STASH_API_KEY in config file or environment.")
    print("  Get your API key from: Stash -> Settings -> Security -> API Key")
if SERVER_ID:
    print(f"  Server ID: {SERVER_ID}")
if TAG_GROUPS:
    print(f"  Tag groups: {', '.join(TAG_GROUPS)}")
if FAVORITE_TAG:
    print(f"  Favorite tag: {FAVORITE_TAG}")
if LATEST_GROUPS:
    print(f"  Latest groups: {', '.join(LATEST_GROUPS)}")
print(f"  Banner: mode={BANNER_MODE}, pool={BANNER_POOL_SIZE}" + (f", tags=[{', '.join(BANNER_TAGS)}]" if BANNER_TAGS else ""))

# Auto-generate SERVER_ID if not set, or normalize old dashless format
if not SERVER_ID:
    SERVER_ID = generate_server_id()
    print(f"  Generated new Server ID: {SERVER_ID}")
    try:
        save_server_id_to_config(CONFIG_FILE, SERVER_ID)
        print(f"  Saved Server ID to {CONFIG_FILE}")
        _config_defined_keys.add("SERVER_ID")
    except Exception as e:
        print(f"  Warning: Could not save Server ID to config: {e}")
        print("  Server ID will be regenerated on next restart unless saved manually.")
else:
    normalized = normalize_server_id(SERVER_ID)
    if normalized != SERVER_ID:
        print(f"  Upgraded Server ID to UUID format: {normalized}")
        SERVER_ID = normalized
        try:
            save_server_id_to_config(CONFIG_FILE, SERVER_ID)
            print(f"  Saved updated Server ID to {CONFIG_FILE}")
        except Exception as e:
            print(f"  Warning: Could not save updated Server ID to config: {e}")

# Load or generate ACCESS_TOKEN (persistent across restarts so clients keep working)
ACCESS_TOKEN = _config.get("ACCESS_TOKEN", "") if _config else ""
if not ACCESS_TOKEN:
    ACCESS_TOKEN = str(uuid.uuid4())
    print(f"  Generated new Access Token")
    try:
        save_config_value(CONFIG_FILE, "ACCESS_TOKEN", ACCESS_TOKEN, "Persistent access token for client sessions (auto-generated)")
        print(f"  Saved Access Token to {CONFIG_FILE}")
    except Exception as e:
        print(f"  Warning: Could not save Access Token to config: {e}")

# Stable user UUID derived from server ID + username (required by strict Jellyfin SDK clients)
import uuid as _uuid_mod
USER_ID = str(_uuid_mod.uuid5(_uuid_mod.UUID(SERVER_ID.replace("-", "").ljust(32, "0")[:32]), SJS_USER or "user"))

# Session management for cookie-based auth — lives on proxy.runtime now
# but seed an initial None so the runtime.publish() call below has a value.
STASH_SESSION = None

# Image cache for resized studio/performer images (prevents repeated processing)
IMAGE_CACHE = {}  # Key: (item_id, target_size), Value: (bytes, content_type)

# Publish every config-derived value + key mutable state into proxy.runtime
# so extracted modules have a single, authoritative source to read from.
# Dual-writes during the Phase 0.6 refactor window — the monolith keeps its
# own module-level copies too until every consumer is extracted. When that
# lands we remove the duplicates and this becomes the sole owner.
import proxy.runtime as _runtime
_runtime.publish(
    # Stash connection
    STASH_URL=STASH_URL,
    STASH_API_KEY=STASH_API_KEY,
    STASH_GRAPHQL_PATH=STASH_GRAPHQL_PATH,
    STASH_VERIFY_TLS=STASH_VERIFY_TLS,
    STASH_TIMEOUT=STASH_TIMEOUT,
    STASH_RETRIES=STASH_RETRIES,
    STASH_SESSION=STASH_SESSION,
    # Proxy bind + identity
    PROXY_BIND=PROXY_BIND,
    PROXY_PORT=PROXY_PORT,
    UI_PORT=UI_PORT,
    SERVER_NAME=SERVER_NAME,
    SERVER_ID=SERVER_ID,
    # Client auth
    SJS_USER=SJS_USER,
    SJS_PASSWORD=SJS_PASSWORD,
    ACCESS_TOKEN=ACCESS_TOKEN,
    # Libraries
    TAG_GROUPS=TAG_GROUPS,
    FAVORITE_TAG=FAVORITE_TAG,
    LATEST_GROUPS=LATEST_GROUPS,
    BANNER_MODE=BANNER_MODE,
    BANNER_POOL_SIZE=BANNER_POOL_SIZE,
    BANNER_TAGS=BANNER_TAGS,
    # Feature toggles
    ENABLE_FILTERS=ENABLE_FILTERS,
    ENABLE_IMAGE_RESIZE=ENABLE_IMAGE_RESIZE,
    ENABLE_TAG_FILTERS=ENABLE_TAG_FILTERS,
    ENABLE_ALL_TAGS=ENABLE_ALL_TAGS,
    REQUIRE_AUTH_FOR_CONFIG=REQUIRE_AUTH_FOR_CONFIG,
    # Pagination / image cache
    DEFAULT_PAGE_SIZE=DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE=MAX_PAGE_SIZE,
    IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
    IMAGE_CACHE=IMAGE_CACHE,
    # Logging
    LOG_DIR=LOG_DIR,
    LOG_FILE=LOG_FILE,
    LOG_LEVEL=LOG_LEVEL,
    LOG_MAX_SIZE_MB=LOG_MAX_SIZE_MB,
    LOG_BACKUP_COUNT=LOG_BACKUP_COUNT,
    # IP ban state (BANNED_IPS is a live set; we publish the reference so
    # writers in either place mutate the same object)
    BANNED_IPS=BANNED_IPS,
    BAN_THRESHOLD=BAN_THRESHOLD,
    BAN_WINDOW_MINUTES=BAN_WINDOW_MINUTES,
    # Config paths + loaded data
    CONFIG_FILE=CONFIG_FILE,
    LOCAL_CONFIG_FILE=LOCAL_CONFIG_FILE,
    config=_config,
    config_defined_keys=_config_defined_keys,
    config_sections=_config_sections,
    # Migration
    MIGRATION_PERFORMED=MIGRATION_PERFORMED,
    MIGRATION_LOG=MIGRATION_LOG,
    # Identity
    JELLYFIN_VERSION=JELLYFIN_VERSION,
    USER_ID=USER_ID,
    # Live mutable state used by ui_api_config
    env_overrides=_env_overrides,
)

# Menu icons as simple SVG graphics (styled similar to Stash's icons)
# These are served for root-scenes, root-studios, root-performers, root-groups
# Using portrait 2:3 aspect ratio (400x600) for Infuse's folder tiles
# MENU_ICONS + endpoint_image live in proxy/endpoints/images.py.
from proxy.endpoints.images import MENU_ICONS, endpoint_image  # noqa: F401

# --- Web UI HTML/CSS/JS ---
# Extracted to proxy/ui/templates/index.html (Phase 0.6 / plan §9.1).
# Loaded once at import time so per-request handlers stay fast.
from pathlib import Path as _HtmlPath
_WEB_UI_TEMPLATE = _HtmlPath(__file__).parent / 'proxy' / 'ui' / 'templates' / 'index.html'
WEB_UI_HTML = _WEB_UI_TEMPLATE.read_text()

# --- Logging Setup ---
from proxy.logging_setup import setup_logging
logger = setup_logging(
    log_level=LOG_LEVEL,
    log_file=LOG_FILE,
    log_dir=LOG_DIR,
    log_max_size_mb=LOG_MAX_SIZE_MB,
    log_backup_count=LOG_BACKUP_COUNT,
)

# --- Middleware for Request Logging ---
# Stream-tracking state lives in proxy/state/streams.py. Monolith imports
# the module (not bare names) and re-exports aliases so legacy call sites
# keep working; new writes should go through `_streams_mod.X`.
from proxy.state import streams as _streams_mod
_active_streams = _streams_mod._active_streams
_client_streams = _streams_mod._client_streams
_recently_stopped = _streams_mod._recently_stopped
_stream_positions = _streams_mod._stream_positions
should_count_as_new_stream = _streams_mod.should_count_as_new_stream
mark_stream_stopped = _streams_mod.mark_stream_stopped
cancel_client_streams = _streams_mod.cancel_client_streams
STREAM_RESUME_THRESHOLD = _streams_mod.STREAM_RESUME_THRESHOLD
RECENTLY_STOPPED_GRACE = _streams_mod.RECENTLY_STOPPED_GRACE
STREAM_COUNT_COOLDOWN = _streams_mod.STREAM_COUNT_COOLDOWN
STREAM_START_GAP = _streams_mod.STREAM_START_GAP
STREAM_START_THRESHOLD = _streams_mod.STREAM_START_THRESHOLD

# --- Proxy Statistics Tracking ---
# Stats live in proxy/state/stats.py. The monolith imports the module
# (not individual names) so mutations from either side stay coherent.
# Back-compat aliases: a few existing monolith call sites (middleware,
# UI endpoints) reference the old bare names; re-export them until each
# call site is migrated to `_stats_mod.X`.
from proxy.state import stats as _stats_mod
_proxy_stats = _stats_mod._proxy_stats      # same dict, mutated in place
load_proxy_stats = _stats_mod.load_proxy_stats
save_proxy_stats = _stats_mod.save_proxy_stats
maybe_save_stats = _stats_mod.maybe_save_stats
reset_daily_stats_if_needed = _stats_mod.reset_daily_stats_if_needed
record_play_count = _stats_mod.record_play_count
record_auth_attempt = _stats_mod.record_auth_attempt
get_top_played_scenes = _stats_mod.get_top_played_scenes
get_proxy_stats = _stats_mod.get_proxy_stats
STATS_FILE = _stats_mod._stats_file()

# should_count_as_new_stream / mark_stream_stopped / cancel_client_streams
# now live in proxy/state/streams.py. Back-compat aliases above keep
# existing monolith call sites working.

# Auth middleware, IP ban tracking, and public-endpoint allowlist live in
# proxy/middleware/auth.py. All state goes through proxy.runtime; the
# _ip_failures dict stays local to the middleware module.
from proxy.middleware.auth import (  # noqa: F401
    AuthenticationMiddleware,
    PUBLIC_ENDPOINTS,
    PUBLIC_PREFIXES,
    get_client_ip,
    record_auth_failure,
    save_banned_ips_to_config,
    clear_ip_failures,
)

# CaseInsensitivePathMiddleware lives in proxy/middleware/paths.py.
from proxy.middleware.paths import CaseInsensitivePathMiddleware


# get_scene_info + get_scene_title live in proxy/stash/scene.py.
from proxy.stash.scene import get_scene_info, get_scene_title  # noqa: F401

# RequestLoggingMiddleware lives in proxy/middleware/logging.py.
# It reads stream state from proxy.state.streams and stats from
# proxy.state.stats, and fetches scene metadata via
# proxy.stash.scene.get_scene_info.
from proxy.middleware.logging import RequestLoggingMiddleware  # noqa: F401

# --- Stash GraphQL Client ---
# Moved to proxy/stash/client.py. State lives on proxy.runtime (no
# monolith-local STASH_SESSION/STASH_VERSION/STASH_CONNECTED needed).
# The TTLCache for Stash health probes is owned by the client module.
from proxy.stash.client import (  # noqa: F401
    stash_query,
    get_stash_session,
    check_stash_connection,
    check_stash_connection_cached,
)
# Keep a GRAPHQL_URL module-level alias for any remaining monolith log
# lines; client module writes runtime.GRAPHQL_URL on first query.
GRAPHQL_URL = f"{_runtime.STASH_URL.rstrip('/')}{_runtime.STASH_GRAPHQL_PATH}"
_runtime.GRAPHQL_URL = GRAPHQL_URL

# Saved-filter helpers + endpoint_items + endpoint_item_details + transform_saved_filter_to_graphql
# live in proxy/endpoints/items.py.
from proxy.endpoints.items import (  # noqa: F401
    is_sort_only_filter,
    stash_get_saved_filters,
    format_filters_folder,
    format_saved_filter_item,
    transform_saved_filter_to_graphql,
    endpoint_items,
    endpoint_item_details,
)

# --- Jellyfin Models & Helpers ---
# Note: SERVER_ID and ACCESS_TOKEN are configured/persisted at startup

# ID converters live in proxy/util/ids.py (Phase 0.6 leaf).
from proxy.util.ids import make_guid, extract_numeric_id, get_numeric_id  # noqa: F401

# Favorites + played/unplayed + tag cache live in
# proxy/endpoints/user_actions.py and proxy/stash/tags.py.
from proxy.endpoints.user_actions import (  # noqa: F401
    endpoint_user_favorites,
    endpoint_user_item_favorite,
    endpoint_user_item_unfavorite,
    endpoint_user_played_items,
    endpoint_user_unplayed_items,
)
from proxy.stash.tags import get_or_create_tag as _get_or_create_tag  # noqa: F401



# Scene mapping + favorite helpers live in proxy/mapping/scene.py.
from proxy.mapping.scene import (  # noqa: F401
    format_jellyfin_item,
    is_scene_favorite as _is_scene_favorite,
    is_group_favorite as _is_group_favorite,
)

# --- API Endpoints ---

# System-info / root / authentication / user-identity endpoints live in
# proxy/endpoints/system.py and proxy/endpoints/users.py.
from proxy.endpoints.system import (  # noqa: F401
    endpoint_root,
    endpoint_system_info,
    endpoint_public_info,
    derive_local_address as _derive_local_address,
)
from proxy.endpoints.users import (  # noqa: F401
    endpoint_authenticate_by_name,
    endpoint_users,
    endpoint_user_by_id,
    endpoint_user_me,
    endpoint_user_image,
    parse_emby_auth_header,
)








# _build_user_dto lives in proxy/mapping/user.py now; keep a local alias
# under its old name so every existing call site in the monolith works.
from proxy.mapping.user import build_user_dto as _build_user_dto  # noqa: E402, F401




# Home-tab / library endpoints moved to proxy/endpoints/views.py.
from proxy.endpoints.views import (  # noqa: F401
    endpoint_user_views,
    endpoint_virtual_folders,
    endpoint_shows_nextup,
    endpoint_latest_items,
    endpoint_user_items_resume,
    endpoint_sessions,
)

# Stub endpoints moved to proxy/endpoints/stubs.py.
from proxy.endpoints.stubs import (  # noqa: F401
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


# Display-preferences + WebSocket endpoints live in proxy/endpoints/misc.py.
from proxy.endpoints.misc import endpoint_display_preferences, endpoint_websocket  # noqa: F401

# Search + list endpoints + query helpers live in proxy/endpoints/search.py
# and proxy/stash/query_helpers.py.
from proxy.stash.query_helpers import (  # noqa: F401
    get_stash_sort_params,
    scene_filter_clause_for_parent as _scene_filter_clause_for_parent,
)
from proxy.endpoints.search import (  # noqa: F401
    endpoint_items_counts,
    endpoint_items_filters,
    endpoint_genres,
    endpoint_persons,
    endpoint_studios,
    endpoint_search_hints,
)



# PlaybackInfo lives in proxy/endpoints/playback.py.
from proxy.endpoints.playback import endpoint_playback_info  # noqa: F401

# get_numeric_id now lives in proxy/util/ids.py and is imported at the top.

from proxy.stash.client import fetch_from_stash  # noqa: F401

# Stream / download / subtitle endpoints live in proxy/endpoints/stream.py.
from proxy.endpoints.stream import (  # noqa: F401
    endpoint_stream,
    endpoint_download,
    endpoint_subtitle,
)


# endpoint_user_items_resume moved to proxy/endpoints/views.py



































# --- App construction, routes, error handlers, and UI server ---
# All of these live in proxy/app.py now.
from proxy.app import app, ui_app, routes, ui_routes, SuppressDisconnectFilter  # noqa: F401
from proxy.errors import (  # noqa: F401
    StashUnavailable, StashError, BadRequest,
    _error_json,
)

# --- Web UI Server ---
PROXY_RUNNING = False  # Track if proxy is running
PROXY_START_TIME = None  # Track when proxy started

# Global reference for restart functionality
_shutdown_event = None
_restart_requested = False


# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="stash-jellyfin-proxy",
        description="Stash-Jellyfin Proxy Server — serve Stash over the Jellyfin API.",
    )
    parser.add_argument("--config", metavar="PATH", help="Path to base config file (default: stash_jellyfin_proxy.conf beside the script, or $CONFIG_FILE)")
    parser.add_argument("--local-config", metavar="PATH", help="Path to local override config merged on top of --config (default: <base>.local.conf, or $LOCAL_CONFIG_FILE)")
    parser.add_argument("--host", metavar="HOST", help="Override PROXY_BIND from config (e.g. 127.0.0.1)")
    parser.add_argument("--port", type=int, metavar="PORT", help="Override PROXY_PORT from config")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL from config")
    parser.add_argument("--debug", action="store_true", help="Shortcut for --log-level DEBUG")
    parser.add_argument("--no-log-file", action="store_true", help="Disable file logging")
    parser.add_argument("--no-ui", action="store_true", help="Disable Web UI server")
    args = parser.parse_args()

    # Apply CLI overrides that take effect after config load.
    if args.host:
        PROXY_BIND = args.host
    if args.port:
        PROXY_PORT = args.port
    if args.log_level:
        level = getattr(logging, args.log_level)
        logger.setLevel(level)
        for handler in logger.handlers:
            handler.setLevel(level)

    # Override logging if --debug flag is set
    if args.debug:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)

    # Remove file handler if --no-log-file is set
    if args.no_log_file:
        logger.handlers = [h for h in logger.handlers if not isinstance(h, (RotatingFileHandler, logging.FileHandler))]

    # Suppress socket disconnect errors (expected during video seeking)
    # These come from both Hypercorn and asyncio when clients disconnect
    hypercorn_error_logger = logging.getLogger("hypercorn.error")
    hypercorn_error_logger.addFilter(SuppressDisconnectFilter())

    # The "socket.send() raised exception" messages come from asyncio, not Hypercorn
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.setLevel(logging.CRITICAL)  # Only show critical asyncio errors

    logger.info(f"--- Stash-Jellyfin Proxy v6.02 ---")

    stash_ok = check_stash_connection()
    if not stash_ok:
        logger.warning("Could not connect to Stash. Proxy will start but streaming will not work until Stash is reachable.")
        logger.warning(f"Check STASH_URL ({STASH_URL}) and STASH_API_KEY settings.")

    PROXY_RUNNING = True
    PROXY_START_TIME = time.time()

    # Load stats from file
    load_proxy_stats()

    # Configure proxy server
    proxy_config = Config()
    proxy_config.bind = [f"{PROXY_BIND}:{PROXY_PORT}"]
    proxy_config.accesslog = logging.getLogger("hypercorn.access")
    proxy_config.access_log_format = "%(h)s %(l)s %(u)s %(t)s \"%(r)s\" %(s)s %(b)s"
    proxy_config.errorlog = logging.getLogger("hypercorn.error")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    shutdown_event = asyncio.Event()

    # Update module-level reference for restart endpoint
    import __main__
    __main__._shutdown_event = shutdown_event

    def signal_handler():
        logger.info("Shutdown signal received...")
        # Save stats before shutting down
        save_proxy_stats()
        shutdown_event.set()

    async def run_servers():
        """Run both proxy and UI servers with graceful shutdown."""
        # Set up signal handlers (add_signal_handler not supported on Windows)
        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, signal_handler)

        tasks = [serve(app, proxy_config, shutdown_trigger=shutdown_event.wait)]

        # Start UI server if enabled
        if UI_PORT > 0 and not args.no_ui:
            ui_config = Config()
            ui_config.bind = [f"{PROXY_BIND}:{UI_PORT}"]
            ui_config.accesslog = None  # Disable access logging for UI
            ui_config.errorlog = logging.getLogger("hypercorn.error")
            tasks.append(serve(ui_app, ui_config, shutdown_trigger=shutdown_event.wait))
            logger.info(f"Web UI: http://{PROXY_BIND}:{UI_PORT}")

        logger.info("Starting Hypercorn server...")
        await asyncio.gather(*tasks)
        logger.info("Servers stopped.")

    try:
        loop.run_until_complete(run_servers())
    except KeyboardInterrupt:
        pass
    except OSError as e:
        if e.errno == 98:  # Address already in use
            logger.error(f"ABORTING: Port already in use. Is another instance running?")
            logger.error(f"  Proxy port {PROXY_PORT} or UI port {UI_PORT} is already bound.")
            logger.error(f"  Try: lsof -i :{PROXY_PORT} or lsof -i :{UI_PORT}")
        else:
            logger.error(f"ABORTING: Network error: {e}")
        sys.exit(1)

    # Check if restart was requested (must happen after event loop exits)
    if _restart_requested:
        logger.info("Executing restart...")
        time.sleep(0.5)  # Brief pause before restart

        # Detect if running in Docker (/.dockerenv exists or CONFIG_FILE points to /config)
        in_docker = os.path.exists("/.dockerenv") or CONFIG_FILE.startswith("/config")

        if in_docker:
            # In Docker, exit cleanly and let Docker's restart policy handle it
            logger.info("Docker detected - exiting for container restart")
            sys.exit(0)
        else:
            # Outside Docker, use os.execv for in-place restart
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
