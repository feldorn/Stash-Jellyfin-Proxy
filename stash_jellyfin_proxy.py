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
)

def save_config_value(config_file, key, value, comment=None):
    """Save a key=value to config file, updating existing entry or adding new one."""
    if not os.path.isfile(config_file):
        with open(config_file, 'w') as f:
            if comment:
                f.write(f'# {comment}\n')
            f.write(f'{key} = {value}\n')
        return True

    with open(config_file, 'r') as f:
        lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#') and key in stripped and '=' in stripped:
            new_lines.append(f'{key} = {value}\n')
            updated = True
        elif stripped.startswith(key) and '=' in stripped:
            new_lines.append(f'{key} = {value}\n')
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        prefix = f'\n# {comment}\n' if comment else '\n'
        new_lines.append(f'{prefix}{key} = {value}\n')

    with open(config_file, 'w') as f:
        f.writelines(new_lines)
    return True

def save_server_id_to_config(config_file, server_id):
    """Save SERVER_ID to config file."""
    return save_config_value(config_file, "SERVER_ID", server_id, "Server identification (auto-generated)")

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


def get_config_section(section_name):
    """Return a dict of {key: value} for the named section, or {} if the
    section is not present. Module-level accessor so mappers/endpoints can
    read per-profile settings without touching the global dict."""
    return dict(_config_sections.get(section_name, {}))


def get_config_sections_by_prefix(prefix):
    """Return {section_name: body} for every section starting with `prefix`.
    e.g. get_config_sections_by_prefix("player.") yields every player
    profile block."""
    return {
        name: dict(body)
        for name, body in _config_sections.items()
        if name.startswith(prefix)
    }

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
)

# Menu icons as simple SVG graphics (styled similar to Stash's icons)
# These are served for root-scenes, root-studios, root-performers, root-groups
# Using portrait 2:3 aspect ratio (400x600) for Infuse's folder tiles
MENU_ICONS = {
    "root-scenes": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
        <rect width="400" height="600" fill="#1a1a2e"/>
        <circle cx="200" cy="280" r="100" fill="none" stroke="#4a90d9" stroke-width="12"/>
        <polygon points="170,230 170,330 250,280" fill="#4a90d9"/>
    </svg>""",
    "root-studios": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
        <rect width="400" height="600" fill="#1a1a2e"/>
        <rect x="80" y="220" width="240" height="160" rx="10" fill="none" stroke="#4a90d9" stroke-width="12"/>
        <circle cx="200" cy="300" r="40" fill="#4a90d9"/>
        <rect x="120" y="380" width="160" height="24" fill="#4a90d9"/>
    </svg>""",
    "root-performers": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
        <rect width="400" height="600" fill="#1a1a2e"/>
        <circle cx="200" cy="220" r="70" fill="none" stroke="#4a90d9" stroke-width="12"/>
        <path d="M80,420 Q80,320 200,320 Q320,320 320,420 L320,440 L80,440 Z" fill="none" stroke="#4a90d9" stroke-width="12"/>
    </svg>""",
    "root-groups": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
        <rect width="400" height="600" fill="#1a1a2e"/>
        <rect x="80" y="200" width="100" height="160" rx="6" fill="none" stroke="#4a90d9" stroke-width="10"/>
        <rect x="150" y="240" width="100" height="160" rx="6" fill="none" stroke="#4a90d9" stroke-width="10"/>
        <rect x="220" y="280" width="100" height="160" rx="6" fill="none" stroke="#4a90d9" stroke-width="10"/>
    </svg>""",
    "root-tag": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
        <rect width="400" height="600" fill="#1a1a2e"/>
        <path d="M120,220 L280,220 L320,300 L200,420 L80,300 Z" fill="none" stroke="#4a90d9" stroke-width="12" stroke-linejoin="round"/>
        <circle cx="160" cy="280" r="20" fill="#4a90d9"/>
    </svg>""",
    "root-tags": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
        <rect width="400" height="600" fill="#1a1a2e"/>
        <path d="M100,200 L240,200 L280,260 L160,380 L60,260 Z" fill="none" stroke="#4a90d9" stroke-width="10" stroke-linejoin="round"/>
        <path d="M140,240 L280,240 L320,300 L200,420 L100,300 Z" fill="none" stroke="#4a90d9" stroke-width="10" stroke-linejoin="round"/>
        <circle cx="130" cy="250" r="16" fill="#4a90d9"/>
        <circle cx="170" cy="290" r="16" fill="#4a90d9"/>
    </svg>"""
}

# --- Web UI HTML/CSS/JS ---
# Extracted to proxy/ui/templates/index.html (Phase 0.6 / plan §9.1).
# Loaded once at import time so per-request handlers stay fast.
from pathlib import Path as _HtmlPath
_WEB_UI_TEMPLATE = _HtmlPath(__file__).parent / 'proxy' / 'ui' / 'templates' / 'index.html'
WEB_UI_HTML = _WEB_UI_TEMPLATE.read_text()

# --- Logging Setup ---
def setup_logging():
    """Configure logging with both console and file handlers."""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # Determine log level
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
    }
    log_level = level_map.get(LOG_LEVEL.upper(), logging.INFO)
    print(f"  Log level: {LOG_LEVEL.upper()} ({log_level})")

    # Create logger
    log = logging.getLogger("stash-jellyfin-proxy")
    log.setLevel(log_level)
    log.propagate = False  # Prevent propagation to root logger

    # Clear any existing handlers
    log.handlers = []

    # Console handler (always enabled). sys.stdout is reconfigured to UTF-8
    # at import time on Windows, so StreamHandler(sys.stdout) is safe for emoji.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))
    console_handler.setLevel(log_level)
    log.addHandler(console_handler)

    # File handler (if LOG_FILE is set)
    if LOG_FILE:
        try:
            # Build full log path
            log_path = os.path.join(LOG_DIR, LOG_FILE) if LOG_DIR else LOG_FILE

            # Ensure log directory exists
            log_dir = os.path.dirname(log_path)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            # Set up rotating file handler (UTF-8 so emoji and non-ASCII scene
            # titles don't crash the logger on Windows locales).
            if LOG_MAX_SIZE_MB > 0:
                max_bytes = LOG_MAX_SIZE_MB * 1024 * 1024
                file_handler = RotatingFileHandler(
                    log_path,
                    maxBytes=max_bytes,
                    backupCount=LOG_BACKUP_COUNT,
                    encoding="utf-8",
                )
            else:
                file_handler = logging.FileHandler(log_path, encoding="utf-8")

            file_handler.setFormatter(logging.Formatter(log_format))
            file_handler.setLevel(log_level)
            log.addHandler(file_handler)

            print(f"  Log file: {os.path.abspath(log_path)}")
        except Exception as e:
            print(f"Warning: Could not set up file logging: {e}")

    return log

# Initialize logger (will be reconfigured in main if needed)
logger = setup_logging()

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

def is_sort_only_filter(saved_filter: Dict[str, Any]) -> bool:
    """
    Check if a saved filter only defines sorting (no actual filter criteria).
    Sort-only filters are not useful in Infuse since we can't control sort order.
    Returns True if the filter has no meaningful filtering criteria.
    """
    # Get the object_filter (the actual filtering criteria)
    object_filter = saved_filter.get("object_filter")

    # Parse if string
    if isinstance(object_filter, str):
        try:
            object_filter = json.loads(object_filter)
        except:
            object_filter = {}

    # Null or empty object_filter means no filtering
    if not object_filter or object_filter == {}:
        # Check find_filter for search query
        find_filter = saved_filter.get("find_filter") or {}
        # If there's a search query (q), it's not sort-only
        if find_filter.get("q"):
            return False
        # Only has sort/direction or page/per_page - it's sort-only
        logger.debug(f"Filter '{saved_filter.get('name')}' is sort-only (empty object_filter, no search query)")
        return True

    # Check if object_filter only has empty values
    def has_meaningful_filter(obj):
        """Recursively check if object has any non-empty filter values."""
        if obj is None:
            return False
        if isinstance(obj, dict):
            for key, value in obj.items():
                # Skip pagination/sorting keys
                if key in ('page', 'per_page', 'sort', 'direction'):
                    continue
                if has_meaningful_filter(value):
                    return True
            return False
        if isinstance(obj, list):
            return len(obj) > 0 and any(has_meaningful_filter(v) for v in obj)
        if isinstance(obj, str):
            return len(obj) > 0
        if isinstance(obj, bool):
            return True  # Boolean criteria like "organized: true" is meaningful
        if isinstance(obj, (int, float)):
            return True  # Numeric criteria is meaningful
        return False

    if not has_meaningful_filter(object_filter):
        logger.debug(f"Filter '{saved_filter.get('name')}' is sort-only (no meaningful filter criteria)")
        return True

    return False

def stash_get_saved_filters(mode: str, exclude_sort_only: bool = True) -> List[Dict[str, Any]]:
    """Get saved filters from Stash for a specific mode (SCENES, PERFORMERS, STUDIOS, GROUPS).

    Args:
        mode: Filter mode (SCENES, PERFORMERS, STUDIOS, GROUPS, TAGS)
        exclude_sort_only: If True, exclude filters that only define sorting
    """
    query = """query FindSavedFilters($mode: FilterMode) {
        findSavedFilters(mode: $mode) {
            id
            name
            mode
            find_filter { q page per_page sort direction }
            object_filter
            ui_options
        }
    }"""
    res = stash_query(query, {"mode": mode})
    filters = res.get("data", {}).get("findSavedFilters", [])

    if exclude_sort_only:
        original_count = len(filters)
        filters = [f for f in filters if not is_sort_only_filter(f)]
        skipped = original_count - len(filters)
        if skipped > 0:
            logger.debug(f"Excluded {skipped} sort-only filters for mode {mode}")

    logger.debug(f"Found {len(filters)} saved filters for mode {mode}")
    return filters

# Filter mode mapping: library parent_id -> Stash FilterMode
FILTER_MODE_MAP = {
    "root-scenes": "SCENES",
    "root-performers": "PERFORMERS",
    "root-studios": "STUDIOS",
    "root-groups": "GROUPS",
    "root-tags": "TAGS",
}

def format_filters_folder(parent_id: str) -> Dict[str, Any]:
    """Create a Jellyfin folder item for the FILTERS special folder."""
    filter_mode = FILTER_MODE_MAP.get(parent_id, "SCENES")
    filters_id = f"filters-{filter_mode.lower()}"

    # Get count of saved filters for this mode
    filters = stash_get_saved_filters(filter_mode)
    filter_count = len(filters)

    return {
        "Name": "FILTERS",
        "SortName": "!!!FILTERS",  # Sort to top
        "Id": filters_id,
        "ServerId": SERVER_ID,
        "Type": "BoxSet",
        "IsFolder": True,
        "CollectionType": "movies",
        "ChildCount": filter_count,
        "RecursiveItemCount": filter_count,
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}},
        "PrimaryImageAspectRatio": 0.6667,
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": filters_id
        }
    }

def format_saved_filter_item(saved_filter: Dict[str, Any], parent_id: str) -> Dict[str, Any]:
    """Format a saved filter as a browsable folder item."""
    filter_id = saved_filter.get("id")
    filter_name = saved_filter.get("name", f"Filter {filter_id}")
    filter_mode = saved_filter.get("mode", "SCENES").lower()

    item_id = f"filter-{filter_mode}-{filter_id}"

    return {
        "Name": filter_name,
        "SortName": filter_name,
        "Id": item_id,
        "ServerId": SERVER_ID,
        "Type": "BoxSet",
        "IsFolder": True,
        "CollectionType": "movies",
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}},
        "PrimaryImageAspectRatio": 0.6667,
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": item_id
        }
    }

# --- Jellyfin Models & Helpers ---
# Note: SERVER_ID and ACCESS_TOKEN are configured/persisted at startup

# ID converters live in proxy/util/ids.py (Phase 0.6 leaf).
from proxy.util.ids import make_guid, extract_numeric_id, get_numeric_id  # noqa: F401

_favorite_tag_id_cache = None

def _get_or_create_tag(tag_name: str) -> str:
    """Get a tag ID by name, creating it if it doesn't exist. Caches the result."""
    global _favorite_tag_id_cache
    if _favorite_tag_id_cache:
        return _favorite_tag_id_cache
    try:
        q = """query FindTags($name: String!) { findTags(tag_filter: {name: {value: $name, modifier: EQUALS}}) { tags { id name } } }"""
        res = stash_query(q, {"name": tag_name})
        tags = res.get("data", {}).get("findTags", {}).get("tags", [])
        if tags:
            _favorite_tag_id_cache = tags[0]["id"]
            return _favorite_tag_id_cache
        q = """mutation TagCreate($input: TagCreateInput!) { tagCreate(input: $input) { id name } }"""
        res = stash_query(q, {"input": {"name": tag_name}})
        tag = res.get("data", {}).get("tagCreate")
        if tag:
            _favorite_tag_id_cache = tag["id"]
            logger.info(f"Created favorite tag '{tag_name}' with ID {_favorite_tag_id_cache}")
            return _favorite_tag_id_cache
    except Exception as e:
        logger.error(f"Error getting/creating tag '{tag_name}': {e}")
    return None

# Scene mapping + favorite helpers live in proxy/mapping/scene.py.
from proxy.mapping.scene import (  # noqa: F401
    format_jellyfin_item,
    is_scene_favorite as _is_scene_favorite,
    is_group_favorite as _is_group_favorite,
)

# --- API Endpoints ---

async def endpoint_root(request):
    """Infuse might check root for life."""
    return RedirectResponse(url="/System/Info/Public")

def _derive_local_address(request):
    """Return the externally-visible base URL the client used to reach us.
    Web clients (Fladder, Jellyfin web) parse LocalAddress and will reject
    the server outright if it advertises something unreachable like
    http://0.0.0.0:8096 (the Docker bind). Respect reverse-proxy headers
    so SWAG/nginx setups advertise the public https origin."""
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if fwd_proto and fwd_host:
        return f"{fwd_proto}://{fwd_host}"
    if fwd_host:
        scheme = request.url.scheme or "http"
        return f"{scheme}://{fwd_host}"
    return f"http://{PROXY_BIND}:{PROXY_PORT}"

async def endpoint_system_info(request):
    logger.debug("Providing System Info")
    local_addr = _derive_local_address(request)
    return JSONResponse({
        "ServerName": SERVER_NAME,
        "Version": JELLYFIN_VERSION,
        "Id": SERVER_ID,
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "StartupWizardCompleted": True,
        "SupportsLibraryMonitor": False,
        "WebSocketPortNumber": PROXY_PORT,
        "CompletedInstallations": [],
        "CanSelfRestart": False,
        "CanLaunchWebBrowser": False,
        "HasPendingRestart": False,
        "HasUpdateAvailable": False,
        "IsShuttingDown": False,
        "TranscodingTempPath": "/tmp",
        "LogPath": "/tmp",
        "InternalMetadataPath": "/tmp",
        "CachePath": "/tmp",
        "ProgramDataPath": "/tmp",
        "ItemsByNamePath": "/tmp",
        "LocalAddress": local_addr
    })

async def endpoint_public_info(request):
    return JSONResponse({
        "LocalAddress": _derive_local_address(request),
        "ServerName": SERVER_NAME,
        "Version": JELLYFIN_VERSION,
        "Id": SERVER_ID,
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "StartupWizardCompleted": True
    })

def parse_emby_auth_header(request):
    """Extract Client, Device, DeviceId, Version from Jellyfin/Emby auth headers."""
    info = {"Client": "Jellyfin", "DeviceName": "Unknown", "DeviceId": "", "ApplicationVersion": "0.0.0"}
    auth_header = ""
    for key, value in request.headers.items():
        key_lower = key.lower()
        if key_lower in ("authorization", "x-emby-authorization"):
            auth_header = value
            break
    if auth_header:
        for field, json_key in [("Client", "Client"), ("Device", "DeviceName"), ("DeviceId", "DeviceId"), ("Version", "ApplicationVersion")]:
            match = re.search(rf'{field}="([^"]*)"', auth_header)
            if match:
                info[json_key] = match.group(1)
    return info

async def endpoint_authenticate_by_name(request):
    if request.method == "GET":
        return Response(status_code=405, headers={"Allow": "POST"})

    try:
        data = await request.json()
    except:
        data = {}

    username = data.get("Username", "User")
    pw = data.get("Pw", "")

    logger.info(f"Auth attempt for user: {username}")
    logger.debug(f"Auth password check: input len={len(pw)}, expected len={len(SJS_PASSWORD)}")

    # Accept config password (strip whitespace from both for comparison)
    if pw.strip() == SJS_PASSWORD.strip():
        # Clear any failed auth attempts for this IP on successful login.
        client_ip = get_client_ip(request.scope)
        clear_ip_failures(client_ip)
        logger.debug(f"Cleared auth failure tracking for {client_ip} after successful login")

        record_auth_attempt(success=True)
        logger.info(f"Auth SUCCESS for user {SJS_USER}")
        client_info = parse_emby_auth_header(request)
        client_ip = get_client_ip(request.scope)
        session_id = str(uuid.uuid4())
        auth_response = {
            "User": _build_user_dto(username),
            "SessionInfo": {
                "Id": session_id,
                "UserId": USER_ID,
                "UserName": username,
                "Client": client_info["Client"],
                "DeviceName": client_info["DeviceName"],
                "DeviceId": client_info["DeviceId"],
                "ApplicationVersion": client_info["ApplicationVersion"],
                "RemoteEndPoint": client_ip,
                "IsActive": True,
                "SupportsMediaControl": False,
                "SupportsRemoteControl": False,
                "HasCustomDeviceName": False,
                "LastActivityDate": "2024-01-01T00:00:00.0000000Z",
                "LastPlaybackCheckIn": "0001-01-01T00:00:00.0000000Z",
                "PlayState": {
                    "CanSeek": False,
                    "IsPaused": False,
                    "IsMuted": False,
                    "RepeatMode": "RepeatNone",
                    "PlaybackOrder": "Default",
                    "PositionTicks": 0,
                    "VolumeLevel": 100
                },
                "Capabilities": {
                    "PlayableMediaTypes": ["Audio", "Video"],
                    "SupportedCommands": [],
                    "SupportsMediaControl": False,
                    "SupportsContentUploading": False,
                    "SupportsPersistentIdentifier": True,
                    "SupportsSync": False
                },
                "PlayableMediaTypes": ["Audio", "Video"],
                "AdditionalUsers": [],
                "NowPlayingQueue": [],
                "NowPlayingQueueFullItems": [],
                "SupportedCommands": [],
                "ServerId": SERVER_ID
            },
            "AccessToken": ACCESS_TOKEN,
            "ServerId": SERVER_ID
        }
        auth_json = json.dumps(auth_response, indent=2)
        logger.debug(f"Auth response ({len(auth_json)} bytes): {auth_json[:200]}...")
        try:
            debug_path = os.path.join(os.path.dirname(CONFIG_FILE) if CONFIG_FILE else "/config", "auth_debug.json")
            with open(debug_path, "w") as f:
                f.write(auth_json)
            logger.debug(f"Full auth response written to {debug_path}")
        except Exception as e:
            logger.debug(f"Could not write auth debug file: {e}")
        return JSONResponse(auth_response)
    else:
        record_auth_attempt(success=False)
        logger.warning("Auth FAILED - Invalid Key")
        return JSONResponse({"error": "Invalid Token"}, status_code=401)

async def endpoint_users(request):
    return JSONResponse([_build_user_dto()])

# _build_user_dto lives in proxy/mapping/user.py now; keep a local alias
# under its old name so every existing call site in the monolith works.
from proxy.mapping.user import build_user_dto as _build_user_dto  # noqa: E402, F401


async def endpoint_user_by_id(request):
    return JSONResponse(_build_user_dto())

async def endpoint_user_me(request):
    """Return current user info - same as user_by_id but for /Users/Me endpoint."""
    return JSONResponse(_build_user_dto())

async def endpoint_user_views(request):
    def make_library(name, lib_id):
        return {
            "Name": name,
            "Id": lib_id,
            "ServerId": SERVER_ID,
            "Etag": hashlib.md5(lib_id.encode()).hexdigest()[:16],
            "DateCreated": "2024-01-01T00:00:00.0000000Z",
            "CanDelete": False,
            "CanDownload": False,
            "SortName": name,
            "ExternalUrls": [],
            "Path": f"/{lib_id}",
            "EnableMediaSourceDisplay": False,
            "Taglines": [],
            "Genres": [],
            "PlayAccess": "Full",
            "RemoteTrailers": [],
            "ProviderIds": {},
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "PrimaryImageAspectRatio": 1.0,
            "DisplayPreferencesId": hashlib.md5(lib_id.encode()).hexdigest()[:32],
            "Tags": [],
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "ScreenshotImageTags": [],
            "ImageBlurHashes": {},
            "LocationType": "FileSystem",
            "LockedFields": [],
            "LockData": False,
            "ChildCount": 100,
            "SpecialFeatureCount": 0,
            "UserData": {
                "PlaybackPositionTicks": 0,
                "PlayCount": 0,
                "IsFavorite": False,
                "Played": False,
                "Key": lib_id,
                "UnplayedItemCount": 100
            }
        }

    items = [
        make_library("Scenes", "root-scenes"),
        make_library("Studios", "root-studios"),
        make_library("Performers", "root-performers"),
        make_library("Groups", "root-groups"),
    ]

    if ENABLE_TAG_FILTERS:
        items.append(make_library("Tags", "root-tags"))

    for tag_name in sorted(TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        items.append(make_library(tag_name, tag_id))

    return JSONResponse({
        "Items": items,
        "TotalRecordCount": len(items)
    })

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


async def endpoint_virtual_folders(request):
    # Infuse requests library virtual folders
    folders = [
        {
            "Name": "Scenes",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-scenes"
        },
        {
            "Name": "Studios",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-studios"
        },
        {
            "Name": "Performers",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-performers"
        },
        {
            "Name": "Groups",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-groups"
        }
    ]

    # Add Tags folder if enabled
    if ENABLE_TAG_FILTERS:
        folders.append({
            "Name": "Tags",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-tags"
        })

    # Add tag group folders (sorted alphabetically)
    for tag_name in sorted(TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        folders.append({
            "Name": tag_name,
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": tag_id
        })

    return JSONResponse(folders)

async def endpoint_shows_nextup(request):
    """Return suggested/random scenes as 'Next Up' to populate Swiftfin home page."""
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 20)

    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

    q = f"""query FindScenes($page: Int!, $per_page: Int!) {{
        findScenes(filter: {{page: $page, per_page: $per_page, sort: "random", direction: DESC}}) {{
            findScenes: scenes {{ {scene_fields} }}
        }}
    }}"""
    try:
        res = stash_query(q, {"page": 1, "per_page": limit})
        scenes = res.get("data", {}).get("findScenes", {}).get("findScenes", [])
        items = [format_jellyfin_item(s) for s in scenes]
        logger.debug(f"NextUp returning {len(items)} random suggestions")
        return JSONResponse({"Items": items, "TotalRecordCount": len(items)})
    except Exception as e:
        logger.warning(f"NextUp query failed: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0})

async def endpoint_latest_items(request):
    """Return recently added items for the Infuse home page, personalized by library."""
    # Get parent_id to filter by library
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 16)

    logger.debug(f"Latest items request - ParentId: {parent_id}, Limit: {limit}")

    # Full scene fields for queries
    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

    items = []

    # Check if this library is in LATEST_GROUPS (if LATEST_GROUPS is empty, show all)
    def is_in_latest_groups(parent_id):
        if not LATEST_GROUPS:
            return True
        if parent_id == "root-scenes":
            return "Scenes" in LATEST_GROUPS
        elif parent_id and parent_id.startswith("tag-"):
            tag_slug = parent_id[4:]
            for t in TAG_GROUPS:
                if t.lower().replace(' ', '-') == tag_slug:
                    return t in LATEST_GROUPS
        return not LATEST_GROUPS

    if not is_in_latest_groups(parent_id):
        logger.debug(f"Skipping latest for {parent_id} (not in LATEST_GROUPS)")
        return JSONResponse(items)

    if parent_id == "root-scenes":
        # Return latest scenes (most recently added)
        q = f"""query FindScenes($page: Int!, $per_page: Int!) {{
            findScenes(filter: {{page: $page, per_page: $per_page, sort: "created_at", direction: DESC}}) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"page": 1, "per_page": limit})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id="root-scenes"))

    elif parent_id and parent_id.startswith("tag-"):
        tag_slug = parent_id[4:]
        tag_name = None
        for t in TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break

        if tag_name:
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) {
                    tags { id name }
                }
            }"""
            tag_res = stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])
            tag_id = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_id = t["id"]
                    break

            if tag_id:
                q = f"""query FindScenes($tid: [ID!], $page: Int!, $per_page: Int!) {{
                    findScenes(
                        scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                        filter: {{page: $page, per_page: $per_page, sort: "created_at", direction: DESC}}
                    ) {{
                        scenes {{ {scene_fields} }}
                    }}
                }}"""
                res = stash_query(q, {"tid": [tag_id], "page": 1, "per_page": limit})
                scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                logger.debug(f"Tag '{tag_name}' latest: {len(scenes)} scenes")
                for s in scenes:
                    items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-performers":
        q = """query FindPerformers($page: Int!, $per_page: Int!) {
            findPerformers(filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}) {
                performers { id name image_path scene_count favorite }
            }
        }"""
        res = stash_query(q, {"page": 1, "per_page": limit})
        for p in res.get("data", {}).get("findPerformers", {}).get("performers", []):
            item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": p.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": bool(p.get("favorite")), "Played": False, "Key": f"performer-{p['id']}"}
            }
            item["ImageTags"] = {"Primary": "img"} if p.get("image_path") else {}
            item["ImageBlurHashes"] = {"Primary": {"img": "000000"}} if p.get("image_path") else {}
            items.append(item)

    elif parent_id == "root-studios":
        q = """query FindStudios($page: Int!, $per_page: Int!) {
            findStudios(filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = stash_query(q, {"page": 1, "per_page": limit})
        for s in res.get("data", {}).get("findStudios", {}).get("studios", []):
            item = {
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": s.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"}
            }
            item["ImageTags"] = {"Primary": "img"} if s.get("image_path") else {}
            item["ImageBlurHashes"] = {"Primary": {"img": "000000"}} if s.get("image_path") else {}
            items.append(item)

    elif parent_id == "root-groups":
        q = """query FindMovies($page: Int!, $per_page: Int!) {
            findMovies(filter: {page: $page, per_page: $per_page, sort: "created_at", direction: DESC}) {
                movies { id name scene_count tags { name } }
            }
        }"""
        res = stash_query(q, {"page": 1, "per_page": limit})
        for m in res.get("data", {}).get("findMovies", {}).get("movies", []):
            item = {
                "Name": m["name"],
                "Id": f"group-{m['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": m.get("scene_count", 0),
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": _is_group_favorite(m), "Played": False, "Key": f"group-{m['id']}"}
            }
            items.append(item)

    elif parent_id == "root-tags":
        # Tags don't have a meaningful "latest" concept
        pass

    logger.debug(f"Returning {len(items)} latest items for {parent_id}")
    return JSONResponse(items)

async def endpoint_display_preferences(request):
    prefs_id = request.path_params.get("prefs_id", "usersettings")

    if request.method == "POST":
        return JSONResponse({"Id": prefs_id})

    return JSONResponse({
        "Id": prefs_id,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {
            "homesection0": "smalllibrarytiles",
            "homesection1": "latestmedia",
            "homesection2": "nextup",
            "homesection3": "none",
            "homesection4": "none",
            "homesection5": "none",
            "homesection6": "none",
        },
        "ScrollDirection": "Horizontal",
        "ShowBackdrop": True,
        "RememberSorting": False,
        "ShowSidebar": False
    })

def get_stash_sort_params(request, context="scenes") -> Tuple[str, str]:
    """Map Jellyfin SortBy/SortOrder to Stash sort/direction.
    context: 'scenes' for scene listings, 'folders' for performers/studios/groups/tags."""
    sort_by_raw = request.query_params.get("SortBy") or request.query_params.get("sortBy") or ("PremiereDate" if context == "scenes" else "SortName")
    sort_order = request.query_params.get("SortOrder") or request.query_params.get("sortOrder") or ("Descending" if context == "scenes" else "Ascending")

    sort_by = sort_by_raw.split(",")[0].strip()

    if context == "folders":
        sort_mapping = {
            "sortname": "name", "name": "name",
            "datecreated": "created_at", "premieredate": "created_at",
            "datelastcontentadded": "created_at",
            "random": "random", "communityrating": "rating",
        }
        default_sort = "name"
    else:
        sort_mapping = {
            "sortname": "title", "name": "title",
            "premieredate": "date",
            "datecreated": "created_at",
            "datelastcontentadded": "created_at",
            "dateplayed": "last_played_at",
            "productionyear": "date",
            "random": "random", "runtime": "duration",
            "communityrating": "rating", "playcount": "play_count",
            "criticrating": "rating",
            "resolution": "bitrate",
        }
        default_sort = "date"

    stash_sort = sort_mapping.get(sort_by.lower(), default_sort)
    stash_direction = "ASC" if sort_order == "Ascending" else "DESC"

    logger.debug(f"Sort mapping ({context}): {sort_by_raw} -> {sort_by} -> {stash_sort} {stash_direction}")

    return stash_sort, stash_direction

def transform_saved_filter_to_graphql(object_filter, filter_mode="SCENES"):
    """
    Transform a saved filter's object_filter format to GraphQL query format.

    Saved filters use a complex format like:
        {'is_missing': {'modifier': 'EQUALS', 'value': 'cover'}}
        {'tags': {'value': ['123', '456'], 'modifier': 'INCLUDES'}}
        {'details': {'modifier': 'IS_NULL'}}  # No value for null checks
        {'duration': {'modifier': 'BETWEEN', 'value': 600, 'value2': 1800}}  # Range
        {'date': {'modifier': 'GREATER_THAN', 'value': '2023-01-01'}}  # Date comparison

    GraphQL expects:
        {'is_missing': 'cover'}
        {'tags': {'value': ['123', '456'], 'modifier': INCLUDES}}
        {'details': {'value': '', 'modifier': IS_NULL}}  # Empty string for null checks
        {'duration': {'value': 600, 'value2': 1800, 'modifier': BETWEEN}}  # Range preserved

    Supported modifiers:
        - EQUALS, NOT_EQUALS
        - INCLUDES, INCLUDES_ALL, EXCLUDES
        - IS_NULL, NOT_NULL
        - GREATER_THAN, LESS_THAN
        - BETWEEN (with value and value2)
        - MATCHES_REGEX

    Supported field types:
        - String fields: title, path, details, url, code, director, phash
        - Boolean fields: organized, interactive, performer_favorite, has_markers
        - Integer fields: rating100, o_counter, play_count, file_count
        - Duration fields: duration (in seconds), resume_time
        - Date fields: date, created_at, updated_at
        - Resolution fields: resolution (enum: VERY_LOW, LOW, R360P, R480P, R720P, R1080P, R1440P, FOUR_K, FIVE_K, etc.)
        - Hierarchical fields: tags, performers, studios, movies/groups
    """
    if not object_filter or not isinstance(object_filter, dict):
        return {}

    result = {}

    # Fields that should be passed as simple booleans (not wrapped in modifier structure)
    BOOLEAN_FIELDS = {'organized', 'interactive', 'performer_favorite', 'has_markers',
                      'ignore_auto_tag', 'favorite', 'is_missing'}

    # Fields that use IntCriterionInput (value/value2/modifier structure)
    INT_CRITERION_FIELDS = {'rating100', 'o_counter', 'play_count', 'file_count',
                            'width', 'height', 'framerate', 'bitrate', 'duration',
                            'resume_time', 'tag_count', 'performer_count', 'scene_count',
                            'gallery_count', 'marker_count', 'image_count'}

    # Fields that use date comparison
    DATE_FIELDS = {'date', 'created_at', 'updated_at', 'last_played_at', 'birthdate', 'death_date'}

    # Fields that use HierarchicalMultiCriterionInput
    HIERARCHICAL_FIELDS = {'tags', 'performers', 'studios', 'movies', 'groups', 'performer_tags'}

    # Fields that use MultiCriterionInput (IDs with modifier)
    MULTI_CRITERION_FIELDS = {'galleries', 'scenes', 'parents', 'children'}

    for key, value in object_filter.items():
        if value is None:
            continue

        # Handle nested filter groups (AND, OR, NOT)
        if key in ('AND', 'OR', 'NOT'):
            if isinstance(value, list):
                transformed = [transform_saved_filter_to_graphql(v, filter_mode) for v in value]
                # Filter out empty dicts from the list
                transformed = [t for t in transformed if t]
                if transformed:
                    result[key] = transformed
            elif isinstance(value, dict):
                transformed = transform_saved_filter_to_graphql(value, filter_mode)
                if transformed:
                    result[key] = transformed
            continue

        # Handle simple string fields that don't need transformation
        if isinstance(value, str):
            result[key] = value
            continue

        # Handle boolean fields
        if isinstance(value, bool):
            result[key] = value
            continue

        # Handle integer fields
        if isinstance(value, (int, float)):
            result[key] = value
            continue

        # Handle list of simple values
        if isinstance(value, list):
            result[key] = value
            continue

        # Handle dict with modifier/value structure
        if isinstance(value, dict):
            modifier = value.get('modifier')
            val = value.get('value')
            val2 = value.get('value2')  # For BETWEEN modifier

            # Special case: is_missing just needs the string value
            if key == 'is_missing' and modifier == 'EQUALS':
                result[key] = val
                continue

            # Handle IS_NULL and NOT_NULL modifiers - they need an empty string value
            if modifier in ('IS_NULL', 'NOT_NULL'):
                result[key] = {'value': '', 'modifier': modifier}
                continue

            # Handle BETWEEN modifier (ranges) - preserve value2
            if modifier == 'BETWEEN':
                if val is not None and val2 is not None:
                    # Ensure numeric values are properly typed
                    try:
                        if key in INT_CRITERION_FIELDS or key in DATE_FIELDS:
                            if key in DATE_FIELDS:
                                # Keep dates as strings
                                result[key] = {'value': val, 'value2': val2, 'modifier': modifier}
                            else:
                                result[key] = {'value': int(val) if not isinstance(val, int) else val,
                                             'value2': int(val2) if not isinstance(val2, int) else val2,
                                             'modifier': modifier}
                        else:
                            result[key] = {'value': val, 'value2': val2, 'modifier': modifier}
                    except (ValueError, TypeError):
                        result[key] = {'value': val, 'value2': val2, 'modifier': modifier}
                    continue

            # Handle comparison modifiers (GREATER_THAN, LESS_THAN)
            if modifier in ('GREATER_THAN', 'LESS_THAN', 'EQUALS', 'NOT_EQUALS'):
                if val is not None:
                    # Handle nested value objects like {'value': 1} -> 1
                    if isinstance(val, dict) and 'value' in val and len(val) == 1:
                        val = val['value']

                    # Convert string booleans to actual booleans
                    if isinstance(val, str):
                        if val.lower() == 'true':
                            val = True
                        elif val.lower() == 'false':
                            val = False

                    # For simple boolean fields with EQUALS modifier, pass boolean directly
                    if key in BOOLEAN_FIELDS and isinstance(val, bool) and modifier == 'EQUALS':
                        result[key] = val
                        continue

                    # For integer fields, ensure proper typing
                    if key in INT_CRITERION_FIELDS and not isinstance(val, bool):
                        try:
                            val = int(val) if isinstance(val, str) else val
                        except (ValueError, TypeError):
                            pass

                    result[key] = {'value': val, 'modifier': modifier}
                    continue

            # For most filter fields with modifier/value, pass through as-is
            if modifier and val is not None:
                # Handle nested value objects like {'value': 1} -> 1
                if isinstance(val, dict) and 'value' in val and len(val) == 1:
                    val = val['value']

                # Convert string booleans to actual booleans
                if isinstance(val, str):
                    if val.lower() == 'true':
                        val = True
                    elif val.lower() == 'false':
                        val = False

                # For simple boolean fields with EQUALS modifier, just pass the boolean directly
                if key in BOOLEAN_FIELDS and isinstance(val, bool) and modifier == 'EQUALS':
                    result[key] = val
                    continue

                # Handle HierarchicalMultiCriterionInput (tags, performers, studios, etc.)
                # Structure: {'items': [{'id': '123', 'label': 'Name'}], 'depth': 0, 'excluded': []}
                # Needs to become: {'value': ['123'], 'modifier': 'INCLUDES_ALL', 'depth': 0, 'excludes': []}
                if key in HIERARCHICAL_FIELDS and isinstance(val, dict) and 'items' in val:
                    items = val.get('items', [])
                    # Extract IDs from items
                    ids = [item.get('id') for item in items if item.get('id')]
                    depth = val.get('depth', 0)
                    # Note: Stash uses 'excluded' but GraphQL expects 'excludes'
                    excludes = val.get('excluded', [])
                    if isinstance(excludes, list):
                        # Extract IDs if excludes contains objects
                        excludes = [e.get('id') if isinstance(e, dict) else e for e in excludes]
                    result[key] = {'value': ids, 'modifier': modifier, 'depth': depth, 'excludes': excludes}
                    continue

                # Handle MultiCriterionInput (just IDs with modifier)
                if key in MULTI_CRITERION_FIELDS and isinstance(val, list):
                    # Extract IDs if val contains objects
                    ids = [v.get('id') if isinstance(v, dict) else v for v in val]
                    result[key] = {'value': ids, 'modifier': modifier}
                    continue

                # Handle resolution (enum type)
                if key == 'resolution':
                    result[key] = {'value': val, 'modifier': modifier}
                    continue

                # Handle orientation/aspect_ratio (enum types)
                if key in ('orientation', 'aspect_ratio'):
                    result[key] = {'value': val, 'modifier': modifier}
                    continue

                # Handle stash_id (with endpoint)
                if key == 'stash_id' and isinstance(val, dict):
                    result[key] = val
                    continue

                # Handle phash_distance (IntCriterionInput with distance field)
                if key == 'phash_distance' and isinstance(val, dict):
                    result[key] = val
                    continue

                result[key] = {'value': val, 'modifier': modifier}
                continue

            # For nested objects without modifier/value, recurse
            if not modifier:
                transformed = transform_saved_filter_to_graphql(value, filter_mode)
                if transformed:
                    result[key] = transformed
                continue

            # If we have modifier but no value, add empty string for value
            # (needed for some modifiers like IS_NULL, NOT_NULL)
            transformed = {'modifier': modifier, 'value': val if val is not None else ''}
            for k, v in value.items():
                if k not in ('modifier', 'value'):
                    transformed[k] = v
            result[key] = transformed

    return result

async def endpoint_items(request):
    user_id = request.path_params.get("user_id")
    # Handle both ParentId and parentId (Infuse uses lowercase)
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    ids = request.query_params.get("Ids") or request.query_params.get("ids")

    # Pagination parameters with validation
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, MAX_PAGE_SIZE))  # Enforce min=1, max=MAX_PAGE_SIZE

    # Sort parameters
    sort_field, sort_direction = get_stash_sort_params(request)

    # Check for PersonIds parameter (Infuse uses this when clicking on a person)
    person_ids = request.query_params.get("PersonIds") or request.query_params.get("personIds")

    # Check for searchTerm parameter (Infuse search functionality)
    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm")

    # Check for Filters parameter (e.g. Filters=IsFavorite used by SenPlayer/Swiftfin Favorites tab)
    filters_param = request.query_params.get("Filters") or request.query_params.get("filters") or ""
    filter_favorites = "isfavorite" in filters_param.lower()

    # Check includeItemTypes - handle both repeated params (includeItemTypes=Movie&includeItemTypes=Series)
    # and comma-separated values (includeItemTypes=Movie,Series)
    raw_type_list = [v for k, v in request.query_params.multi_items() if k.lower() == "includeitemtypes"]
    include_type_list = []
    for val in raw_type_list:
        include_type_list.extend([t.strip() for t in val.split(",") if t.strip()])
    include_types_lower = [t.lower() for t in include_type_list]
    has_movie_type = not include_type_list or "movie" in include_types_lower or "video" in include_types_lower
    restrict_to_movies = has_movie_type and "folder" not in include_types_lower and len(include_type_list) > 0

    # Debug: Log ALL query params (show multi-values properly)
    logger.debug(f"Items endpoint - ALL PARAMS: {dict(request.query_params)}, includeItemTypes={include_type_list}")
    logger.debug(f"Items endpoint - ParentId: {parent_id}, Ids: {ids}, PersonIds: {person_ids}, SearchTerm: {search_term}, StartIndex: {start_index}, Limit: {limit}, Sort: {sort_field} {sort_direction}")

    items = []
    total_count = 0

    # Full scene fields for queries (include performer image_path for People images, captions for subtitles)
    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

    if ids:
        # Specific items requested
        id_list = ids.split(',')
        for iid in id_list:
            q = f"""query FindScene($id: ID!) {{ findScene(id: $id) {{ {scene_fields} }} }}"""
            res = stash_query(q, {"id": iid})
            scene = res.get("data", {}).get("findScene")
            if scene:
                items.append(format_jellyfin_item(scene))
        total_count = len(items)

    elif person_ids:
        # Infuse uses PersonIds parameter to filter by person/performer
        # Extract the numeric ID from person-123 or just 123 format
        person_id = person_ids.split(',')[0]  # Take first if multiple
        if person_id.startswith("person-"):
            performer_id = person_id.replace("person-", "")
        elif person_id.startswith("performer-"):
            performer_id = person_id.replace("performer-", "")
        else:
            performer_id = person_id

        logger.debug(f"PersonIds filter: fetching scenes for performer {performer_id}")

        # Get count for this performer
        count_q = """query CountScenes($pid: [ID!]) {
            findScenes(scene_filter: {performers: {value: $pid, modifier: INCLUDES}}) { count }
        }"""
        count_res = stash_query(count_q, {"pid": [performer_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($pid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{performers: {{value: $pid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"pid": [performer_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"PersonIds filter: returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=f"person-{performer_id}"))

    elif search_term:
        # Handle search from Infuse/Swiftfin - query Stash with the search term
        # Strip any quotes that client might add around the search term
        clean_search = search_term.strip('"\'')

        logger.info(f"Search: '{clean_search}' (types={include_type_list})")

        # Only search for scenes if Movie/Video type is requested (or no type filter)
        # Skip for Series-only or Episode-only requests since Stash only has movie-type content
        if not has_movie_type:
            logger.debug(f"Search skipped - requested types {include_type_list} don't include Movie/Video")
        else:
            # Get count of matching scenes
            count_q = """query CountScenes($q: String!) {
                findScenes(filter: {q: $q}) { count }
            }"""
            count_res = stash_query(count_q, {"q": clean_search})
            total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

            # Calculate page
            page = (start_index // limit) + 1

            # Query Stash with the search term
            q = f"""query FindScenes($q: String!, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                findScenes(filter: {{q: $q, page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                    scenes {{ {scene_fields} }}
                }}
            }}"""
            res = stash_query(q, {"q": clean_search, "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            logger.debug(f"Search '{clean_search}' returned {len(scenes)} scenes (page {page}, total {total_count})")
            for s in scenes:
                items.append(format_jellyfin_item(s))

    elif parent_id and parent_id.startswith("filters-"):
        # List saved filters for a specific mode (filters-scenes, filters-performers, etc.)
        filter_mode = parent_id.replace("filters-", "").upper()
        saved_filters = stash_get_saved_filters(filter_mode)
        total_count = len(saved_filters)

        logger.debug(f"Listing {total_count} saved filters for mode {filter_mode}")

        for sf in saved_filters:
            items.append(format_saved_filter_item(sf, parent_id))

    elif parent_id and parent_id.startswith("filter-"):
        # Apply a saved filter and show results
        # Format: filter-{mode}-{filter_id}
        parts = parent_id.split("-", 2)  # ['filter', 'scenes', '123']
        if len(parts) == 3:
            filter_mode = parts[1].upper()
            filter_id = parts[2]

            # Get the saved filter details
            query = """query FindSavedFilter($id: ID!) {
                findSavedFilter(id: $id) {
                    id name mode
                    find_filter { q page per_page sort direction }
                    object_filter
                }
            }"""
            res = stash_query(query, {"id": filter_id})
            saved_filter = res.get("data", {}).get("findSavedFilter")

            if saved_filter:
                find_filter = saved_filter.get("find_filter") or {}
                object_filter = saved_filter.get("object_filter")

                # Parse object_filter if it's a string (JSON)
                import json
                if isinstance(object_filter, str):
                    try:
                        object_filter = json.loads(object_filter)
                    except Exception as e:
                        logger.warning(f"Failed to parse object_filter JSON: {e}")
                        object_filter = {}

                # Ensure object_filter is a dict, default to empty
                if object_filter is None:
                    object_filter = {}

                logger.debug(f"Applying saved filter '{saved_filter.get('name')}' (id={filter_id}, mode={filter_mode})")
                logger.debug(f"Raw object_filter type: {type(object_filter)}, value: {object_filter}")

                # Transform saved filter format to GraphQL query format
                graphql_filter = transform_saved_filter_to_graphql(object_filter, filter_mode)
                logger.debug(f"Transformed filter: {graphql_filter}")

                # Try querying Stash directly with the filter to see what happens
                # Also log the full saved filter data for debugging
                logger.debug(f"Full saved filter data: {saved_filter}")

                logger.debug(f"Filter find_filter: {find_filter}")
                logger.debug(f"Filter object_filter: {object_filter}")

                # Calculate page and sort
                page = (start_index // limit) + 1
                folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

                # Build the query with the saved filter's criteria
                # Each mode has its own filter type in Stash GraphQL
                if filter_mode == "SCENES":
                    # First get count with filter
                    count_q = """query CountScenes($scene_filter: SceneFilterType) {
                        findScenes(scene_filter: $scene_filter) { count }
                    }"""
                    logger.debug(f"Running count query with scene_filter: {graphql_filter}")
                    count_res = stash_query(count_q, {"scene_filter": graphql_filter})
                    logger.debug(f"Count query response: {count_res}")
                    total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

                    # Get paginated results
                    q = f"""query FindScenes($scene_filter: SceneFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                        findScenes(
                            scene_filter: $scene_filter,
                            filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                        ) {{
                            scenes {{ {scene_fields} }}
                        }}
                    }}"""
                    res = stash_query(q, {
                        "scene_filter": graphql_filter,
                        "page": page,
                        "per_page": limit,
                        "sort": sort_field,
                        "direction": sort_direction
                    })
                    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Saved filter returned {len(scenes)} scenes (page {page}, total {total_count})")
                    for s in scenes:
                        items.append(format_jellyfin_item(s, parent_id=parent_id))

                elif filter_mode == "PERFORMERS":
                    # Count performers with filter
                    count_q = """query CountPerformers($performer_filter: PerformerFilterType) {
                        findPerformers(performer_filter: $performer_filter) { count }
                    }"""
                    count_res = stash_query(count_q, {"performer_filter": graphql_filter})
                    total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

                    # Get paginated performers
                    q = """query FindPerformers($performer_filter: PerformerFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findPerformers(
                            performer_filter: $performer_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            performers { id name image_path scene_count favorite }
                        }
                    }"""
                    res = stash_query(q, {"performer_filter": graphql_filter, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])
                    logger.debug(f"Saved filter returned {len(performers)} performers (page {page}, total {total_count})")
                    for p in performers:
                        performer_item = {
                            "Name": p["name"],
                            "Id": f"performer-{p['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": p.get("scene_count", 0),
                            "RecursiveItemCount": p.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": bool(p.get("favorite")), "Played": False, "Key": f"performer-{p['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(performer_item)

                elif filter_mode == "STUDIOS":
                    # Count studios with filter
                    count_q = """query CountStudios($studio_filter: StudioFilterType) {
                        findStudios(studio_filter: $studio_filter) { count }
                    }"""
                    count_res = stash_query(count_q, {"studio_filter": graphql_filter})
                    total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

                    # Get paginated studios
                    q = """query FindStudios($studio_filter: StudioFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findStudios(
                            studio_filter: $studio_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            studios { id name image_path scene_count }
                        }
                    }"""
                    res = stash_query(q, {"studio_filter": graphql_filter, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
                    logger.debug(f"Saved filter returned {len(studios)} studios (page {page}, total {total_count})")
                    for s in studios:
                        studio_item = {
                            "Name": s["name"],
                            "Id": f"studio-{s['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": s.get("scene_count", 0),
                            "RecursiveItemCount": s.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(studio_item)

                elif filter_mode == "GROUPS":
                    # Count groups/movies with filter
                    count_q = """query CountGroups($group_filter: GroupFilterType) {
                        findGroups(group_filter: $group_filter) { count }
                    }"""
                    count_res = stash_query(count_q, {"group_filter": graphql_filter})
                    total_count = count_res.get("data", {}).get("findGroups", {}).get("count", 0)

                    # Get paginated groups
                    q = """query FindGroups($group_filter: GroupFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findGroups(
                            group_filter: $group_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            groups { id name scene_count }
                        }
                    }"""
                    res = stash_query(q, {"group_filter": graphql_filter, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    groups = res.get("data", {}).get("findGroups", {}).get("groups", [])
                    logger.debug(f"Saved filter returned {len(groups)} groups (page {page}, total {total_count})")
                    for g in groups:
                        group_item = {
                            "Name": g["name"],
                            "Id": f"group-{g['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": g.get("scene_count", 0),
                            "RecursiveItemCount": g.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"group-{g['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(group_item)

                elif filter_mode == "TAGS":
                    # Use fixed page size for Stash queries to avoid pagination misalignment
                    # when Infuse changes limit between requests (e.g., 50 then 31)
                    # Stash pagination: items start at (page-1) * per_page
                    # If we use varying per_page, the offsets won't align with startIndex
                    STASH_PAGE_SIZE = 50  # Fixed internal page size

                    # Calculate which Stash page contains start_index
                    stash_page = (start_index // STASH_PAGE_SIZE) + 1
                    # Offset within that page
                    offset_in_page = start_index % STASH_PAGE_SIZE

                    logger.debug(f"TAGS filter pagination: startIndex={start_index}, limit={limit}, stash_page={stash_page}, offset_in_page={offset_in_page}")

                    q = """query FindTags($tag_filter: TagFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findTags(
                            tag_filter: $tag_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            count
                            tags { id name scene_count image_path favorite }
                        }
                    }"""
                    res = stash_query(q, {"tag_filter": graphql_filter, "page": stash_page, "per_page": STASH_PAGE_SIZE, "sort": folder_sort, "direction": folder_dir})
                    data = res.get("data", {}).get("findTags", {})
                    total_count = data.get("count", 0)
                    all_tags = data.get("tags", [])

                    # Slice from offset_in_page, up to limit items
                    tags = all_tags[offset_in_page:offset_in_page + limit]

                    # If we need more items than remaining in this page, fetch next page too
                    while len(tags) < limit and (stash_page * STASH_PAGE_SIZE) < total_count:
                        stash_page += 1
                        res = stash_query(q, {"tag_filter": graphql_filter, "page": stash_page, "per_page": STASH_PAGE_SIZE, "sort": folder_sort, "direction": folder_dir})
                        next_tags = res.get("data", {}).get("findTags", {}).get("tags", [])
                        tags.extend(next_tags[:limit - len(tags)])

                    # Log first and last 3 tag IDs to help identify duplicates/overlaps
                    first_ids = [t.get("id") for t in tags[:3]] if tags else []
                    last_ids = [t.get("id") for t in tags[-3:]] if len(tags) > 3 else first_ids
                    logger.debug(f"TAGS filter: returning {len(tags)} tags (total {total_count}), first IDs: {first_ids}, last IDs: {last_ids}")
                    for t in tags:
                        tag_item = {
                            "Name": t["name"],
                            "Id": f"tagitem-{t['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": t.get("scene_count", 0),
                            "RecursiveItemCount": t.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": t.get("favorite", False), "Played": False, "Key": f"tagitem-{t['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(tag_item)

                else:
                    logger.warning(f"Unsupported filter mode: {filter_mode}")
            else:
                logger.warning(f"Saved filter not found: {filter_id}")

    elif parent_id == "root-scenes":
        # First get total count
        count_q = """query { findScenes { count } }"""
        count_res = stash_query(count_q)
        scene_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Check if there are saved filters for scenes (only if ENABLE_FILTERS is on)
        has_filters = False
        if ENABLE_FILTERS:
            saved_filters = stash_get_saved_filters("SCENES")
            has_filters = len(saved_filters) > 0

        # On first page, add FILTERS folder at the top if there are saved filters
        # Detect client: Infuse supports folder browsing, others may not
        client_info = parse_emby_auth_header(request)
        client_name = client_info.get("Client", "").lower()
        client_supports_folders = "infuse" in client_name or "senplayer" in client_name
        show_filters = has_filters and (client_supports_folders or not restrict_to_movies)

        filters_added = False
        if start_index == 0 and show_filters:
            items.append(format_filters_folder("root-scenes"))
            filters_added = True

        # Total count includes Filters folder if present
        show_filters_in_count = show_filters
        total_count = scene_count + 1 if show_filters_in_count else scene_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1

        # Reduce per_page when filters folder takes a slot, so total stays within limit
        fetch_limit = limit - 1 if filters_added else limit

        # Then get paginated scenes with sort from request
        q = f"""query FindScenes($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"page": page, "per_page": fetch_limit, "sort": sort_field, "direction": sort_direction})
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s, parent_id="root-scenes"))

    elif parent_id == "root-studios":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

        count_q = """query { findStudios { count } }"""
        count_res = stash_query(count_q)
        studio_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

        has_filters = False
        if ENABLE_FILTERS:
            saved_filters = stash_get_saved_filters("STUDIOS")
            has_filters = len(saved_filters) > 0

        filters_added = False
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-studios"))
            filters_added = True

        total_count = studio_count + 1 if has_filters else studio_count

        page = (start_index // limit) + 1
        fetch_limit = limit - 1 if filters_added else limit

        q = """query FindStudios($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findStudios(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = stash_query(q, {"page": page, "per_page": fetch_limit, "sort": folder_sort, "direction": folder_dir})
        for s in res.get("data", {}).get("findStudios", {}).get("studios", []):
            studio_item = {
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": s.get("scene_count", 0),
                "RecursiveItemCount": s.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"}
            }
            if s.get("image_path"):
                studio_item["ImageTags"] = {"Primary": "img"}
                studio_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            else:
                studio_item["ImageTags"] = {}
            items.append(studio_item)

    elif parent_id and parent_id.startswith("studio-"):
        studio_id = parent_id.replace("studio-", "")

        # Get count for this studio
        count_q = """query CountScenes($sid: [ID!]) {
            findScenes(scene_filter: {studios: {value: $sid, modifier: INCLUDES}}) { count }
        }"""
        count_res = stash_query(count_q, {"sid": [studio_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($sid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{studios: {{value: $sid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"sid": [studio_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Studio {studio_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-performers":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

        # Get total count
        count_q = """query { findPerformers { count } }"""
        count_res = stash_query(count_q)
        performer_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

        # Check if there are saved filters for performers (only if ENABLE_FILTERS is on)
        has_filters = False
        if ENABLE_FILTERS:
            saved_filters = stash_get_saved_filters("PERFORMERS")
            has_filters = len(saved_filters) > 0

        # On first page, add FILTERS folder at the top if there are saved filters
        filters_added = False
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-performers"))
            filters_added = True

        # Total count includes Filters folder if present
        total_count = performer_count + 1 if has_filters else performer_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1
        fetch_limit = limit - 1 if filters_added else limit

        q = """query FindPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findPerformers(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                performers { id name image_path scene_count favorite }
            }
        }"""
        res = stash_query(q, {"page": page, "per_page": fetch_limit, "sort": folder_sort, "direction": folder_dir})
        for p in res.get("data", {}).get("findPerformers", {}).get("performers", []):
            performer_item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": p.get("scene_count", 0),
                "RecursiveItemCount": p.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": bool(p.get("favorite")), "Played": False, "Key": f"performer-{p['id']}"}
            }
            if p.get("image_path"):
                performer_item["ImageTags"] = {"Primary": "img"}
                performer_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            else:
                performer_item["ImageTags"] = {}
            items.append(performer_item)

    elif parent_id and (parent_id.startswith("performer-") or parent_id.startswith("person-")):
        # Handle both performer- (from Performers list) and person- (from People in scene details)
        if parent_id.startswith("performer-"):
            performer_id = parent_id.replace("performer-", "")
        else:
            performer_id = parent_id.replace("person-", "")

        # Get count for this performer
        count_q = """query CountScenes($pid: [ID!]) {
            findScenes(scene_filter: {performers: {value: $pid, modifier: INCLUDES}}) { count }
        }"""
        count_res = stash_query(count_q, {"pid": [performer_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($pid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{performers: {{value: $pid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"pid": [performer_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Performer {performer_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-groups":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

        count_q = """query { findMovies { count } }"""
        count_res = stash_query(count_q)
        group_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)

        has_filters = False
        if ENABLE_FILTERS:
            saved_filters = stash_get_saved_filters("GROUPS")
            has_filters = len(saved_filters) > 0

        filters_added = False
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-groups"))
            filters_added = True

        total_count = group_count + 1 if has_filters else group_count

        FIXED_PAGE_SIZE = 50

        stash_page = (start_index // FIXED_PAGE_SIZE) + 1
        offset_within_page = start_index % FIXED_PAGE_SIZE
        items_needed = limit - 1 if filters_added else limit

        logger.debug(f"Groups pagination: startIndex={start_index}, limit={limit}, stash_page={stash_page}, offset_within_page={offset_within_page}")

        q = """query FindMovies($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findMovies(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                movies { id name scene_count tags { name } }
            }
        }"""

        fetched_movies = []
        current_page = stash_page
        while len(fetched_movies) < offset_within_page + items_needed:
            res = stash_query(q, {"page": current_page, "per_page": FIXED_PAGE_SIZE, "sort": folder_sort, "direction": folder_dir})
            page_movies = res.get("data", {}).get("findMovies", {}).get("movies", [])
            if not page_movies:
                break
            fetched_movies.extend(page_movies)
            current_page += 1
            if current_page > stash_page + 1:
                break

        # Slice to get the items we need
        movies_to_return = fetched_movies[offset_within_page:offset_within_page + items_needed]

        # Log Y-groups for debugging
        y_groups = [m["name"] for m in movies_to_return if m.get("name", "").upper().startswith("Y")]
        if y_groups:
            logger.debug(f"Groups starting with Y in this batch: {y_groups}")

        logger.debug(f"Groups: fetched {len(fetched_movies)} total, returning {len(movies_to_return)} (offset {offset_within_page})")

        for m in movies_to_return:
            group_item = {
                "Name": m["name"],
                "Id": f"group-{m['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": m.get("scene_count", 0),
                "RecursiveItemCount": m.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": _is_group_favorite(m), "Played": False, "Key": f"group-{m['id']}"},
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
            }
            items.append(group_item)

    elif parent_id and parent_id.startswith("group-"):
        group_id = parent_id.replace("group-", "")

        # Get count for this group/movie
        count_q = """query CountScenes($mid: [ID!]) {
            findScenes(scene_filter: {movies: {value: $mid, modifier: INCLUDES}}) { count }
        }"""
        count_res = stash_query(count_q, {"mid": [group_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($mid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{movies: {{value: $mid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"mid": [group_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Group {group_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-tags":
        # Tags folder: show Favorites, All Tags (if enabled), and saved tag filters
        items_count = 0

        # Always show "Favorites" subfolder at the top
        items.append({
            "Name": "Favorites",
            "SortName": "!1-Favorites",  # Sort to top
            "Id": "tags-favorites",
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "IsFolder": True,
            "CollectionType": "movies",
            "ParentId": parent_id,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-favorites"}
        })
        items_count += 1

        # Show "All Tags" if enabled
        if ENABLE_ALL_TAGS:
            items.append({
                "Name": "All Tags",
                "SortName": "!2-All Tags",  # Sort after Favorites
                "Id": "tags-all",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-all"}
            })
            items_count += 1

        # Show saved tag filters
        saved_filters = stash_get_saved_filters("TAGS")
        for sf in saved_filters:
            filter_id = sf.get("id")
            filter_name = sf.get("name", f"Filter {filter_id}")
            item_id = f"filter-tags-{filter_id}"
            items.append({
                "Name": filter_name,
                "SortName": filter_name,
                "Id": item_id,
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
            })
            items_count += 1

        total_count = items_count

    elif parent_id == "tags-favorites":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
        q = """query FindTags($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findTags(tag_filter: {favorite: true}, filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                count
                tags { id name scene_count image_path }
            }
        }"""
        page = (start_index // limit) + 1
        res = stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
        data = res.get("data", {}).get("findTags", {})
        total_count = data.get("count", 0)
        for t in data.get("tags", []):
            tag_item = {
                "Name": t["name"],
                "Id": f"tagitem-{t['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": t.get("scene_count", 0),
                "RecursiveItemCount": t.get("scene_count", 0),
                "ParentId": parent_id,
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": True, "Played": False, "Key": f"tagitem-{t['id']}"}
            }
            tag_item["ImageTags"] = {"Primary": "img"}
            tag_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            items.append(tag_item)

    elif parent_id == "tags-all":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
        q = """query FindTags($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findTags(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                count
                tags { id name scene_count image_path favorite }
            }
        }"""
        page = (start_index // limit) + 1
        res = stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
        data = res.get("data", {}).get("findTags", {})
        total_count = data.get("count", 0)
        for t in data.get("tags", []):
            tag_item = {
                "Name": t["name"],
                "Id": f"tagitem-{t['id']}",
                "ServerId": SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": t.get("scene_count", 0),
                "RecursiveItemCount": t.get("scene_count", 0),
                "ParentId": parent_id,
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": t.get("favorite", False), "Played": False, "Key": f"tagitem-{t['id']}"}
            }
            tag_item["ImageTags"] = {"Primary": "img"}
            tag_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            items.append(tag_item)

    elif parent_id and parent_id.startswith("tagitem-"):
        # Browsing a specific tag - show scenes with this tag
        tag_id = parent_id.replace("tagitem-", "")

        # Get count for scenes with this tag
        count_q = """query CountScenes($tid: [ID!]) {
            findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
        }"""
        count_res = stash_query(count_q, {"tid": [tag_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"tid": [tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Tag {tag_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id and parent_id.startswith("tag-"):
        # Tag-based folder: find scenes with this tag (from TAG_GROUPS config)
        # Extract tag name from parent_id (reverse the slugification)
        tag_slug = parent_id[4:]  # Remove "tag-" prefix

        # Find the matching tag name from TAG_GROUPS config
        tag_name = None
        for t in TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break

        if tag_name:
            # First we need to find the tag ID by name
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) {
                    tags { id name }
                }
            }"""
            tag_res = stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])

            # Find exact match (case-insensitive)
            tag_id = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_id = t["id"]
                    break

            if tag_id:
                # Get count for scenes with this tag
                count_q = """query CountScenes($tid: [ID!]) {
                    findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
                }"""
                count_res = stash_query(count_q, {"tid": [tag_id]})
                total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

                # Calculate page
                page = (start_index // limit) + 1

                q = f"""query FindScenes($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                    findScenes(
                        scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                        filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                    ) {{
                        scenes {{ {scene_fields} }}
                    }}
                }}"""
                res = stash_query(q, {"tid": [tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                logger.debug(f"Tag '{tag_name}' (id={tag_id}) returned {len(scenes)} scenes (page {page}, total {total_count})")
                for s in scenes:
                    items.append(format_jellyfin_item(s, parent_id=parent_id))
            else:
                logger.warning(f"Tag '{tag_name}' not found in Stash")
        else:
            logger.warning(f"Tag slug '{tag_slug}' not found in TAG_GROUPS config")

    elif not parent_id and not ids and not person_ids and not search_term:
        # Global query with no parent - used by clients for home screen, search filters, etc.
        # Distinguish Movie (→ Groups/BoxSets) from Video (→ Scenes) to avoid duplicates.
        # Do NOT return anything for Series/Episode-only requests (Stash only has movie-type content).
        movie_only = bool(include_type_list) and "movie" in include_types_lower and "video" not in include_types_lower
        video_requested = "video" in include_types_lower or not include_type_list  # no type filter = return scenes

        if not has_movie_type:
            logger.debug(f"Global query skipped - requested types {include_type_list} don't include Movie/Video")
        elif movie_only:
            # Banner detection: some clients (SenPlayer) request the home-screen
            # rotating banner with Movie-only + SortBy containing "Random". Return
            # randomized Scenes (with screenshots) instead of Groups for better visuals.
            sort_by_raw = request.query_params.get("SortBy") or request.query_params.get("sortBy") or ""
            is_banner_request = "random" in sort_by_raw.lower() and not filter_favorites
            if is_banner_request:
                banner_scenes = []
                tag_ids = []
                if BANNER_MODE == "tag" and BANNER_TAGS:
                    # Stash's name filter doesn't accept a list; look up each tag individually.
                    for tname in BANNER_TAGS:
                        try:
                            res = stash_query(
                                """query FindTag($n: String!) { findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}) { tags { id name } } }""",
                                {"n": tname},
                            )
                            for t in res.get("data", {}).get("findTags", {}).get("tags", []):
                                if t["name"].lower() == tname.lower():
                                    tag_ids.append(t["id"])
                                    break
                        except Exception as e:
                            logger.warning(f"Banner tag lookup failed for '{tname}': {e}")
                    if not tag_ids:
                        logger.debug(f"Banner mode=tag but no BANNER_TAGS resolved ({BANNER_TAGS}); falling back to recent")

                if BANNER_MODE == "tag" and tag_ids:
                    q = f"""query BannerScenesByTags($tids: [ID!], $per_page: Int!) {{
                        findScenes(
                            scene_filter: {{tags: {{value: $tids, modifier: INCLUDES}}}},
                            filter: {{page: 1, per_page: $per_page, sort: "created_at", direction: DESC}}
                        ) {{ scenes {{ {scene_fields} }} }}
                    }}"""
                    res = stash_query(q, {"tids": tag_ids, "per_page": BANNER_POOL_SIZE})
                    pool = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Banner (tag): pool={len(pool)} from tags={BANNER_TAGS}, picking {limit}")
                else:
                    q = f"""query BannerScenesRecent($per_page: Int!) {{
                        findScenes(filter: {{page: 1, per_page: $per_page, sort: "created_at", direction: DESC}}) {{
                            scenes {{ {scene_fields} }}
                        }}
                    }}"""
                    res = stash_query(q, {"per_page": BANNER_POOL_SIZE})
                    pool = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Banner (recent): pool={len(pool)} newest, picking {limit}")

                if pool:
                    banner_scenes = random.sample(pool, min(limit, len(pool)))
                for s in banner_scenes:
                    items.append(format_jellyfin_item(s))
                total_count = len(items)
                # Skip the Groups branch entirely for banner requests.
                return JSONResponse({
                    "Items": items,
                    "TotalRecordCount": total_count,
                    "StartIndex": start_index,
                })

            # Movie type only → return Groups (BoxSets), not scenes
            folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
            if filter_favorites and FAVORITE_TAG:
                # Return only groups tagged with FAVORITE_TAG (same technique as scenes)
                fav_tag_id = _get_or_create_tag(FAVORITE_TAG)
                if fav_tag_id:
                    count_q = """query CountFavGroups($tid: [ID!]) {
                        findMovies(movie_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
                    }"""
                    count_res = stash_query(count_q, {"tid": [fav_tag_id]})
                    total_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)
                    page = (start_index // limit) + 1
                    q = """query FindFavGroups($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findMovies(
                            movie_filter: {tags: {value: $tid, modifier: INCLUDES}},
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            movies { id name scene_count tags { name } }
                        }
                    }"""
                    res = stash_query(q, {"tid": [fav_tag_id], "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    movies = res.get("data", {}).get("findMovies", {}).get("movies", [])
                    logger.debug(f"Favorite groups query returned {len(movies)} groups (page {page}, total {total_count})")
                else:
                    logger.warning(f"IsFavorite filter requested but could not resolve FAVORITE_TAG '{FAVORITE_TAG}'")
                    movies = []
            elif filter_favorites and not FAVORITE_TAG:
                logger.debug("Movie+IsFavorite: FAVORITE_TAG not configured - returning empty")
                movies = []
                total_count = 0
            else:
                count_q = "query { findMovies { count } }"
                count_res = stash_query(count_q)
                total_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)
                page = (start_index // limit) + 1
                q = """query FindMovies($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                    findMovies(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                        movies { id name scene_count tags { name } }
                    }
                }"""
                res = stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                movies = res.get("data", {}).get("findMovies", {}).get("movies", [])
                logger.debug(f"Global Movie query returned {len(movies)} groups (page {page}, total {total_count})")
            for m in movies:
                items.append({
                    "Name": m["name"],
                    "Id": f"group-{m['id']}",
                    "ServerId": SERVER_ID,
                    "Type": "BoxSet",
                    "IsFolder": True,
                    "CollectionType": "movies",
                    "ChildCount": m.get("scene_count", 0),
                    "PrimaryImageAspectRatio": 0.6667,
                    "BackdropImageTags": [],
                    "ImageTags": {"Primary": "img"},
                    "ImageBlurHashes": {"Primary": {"img": "000000"}},
                    "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": _is_group_favorite(m), "Played": False, "Key": f"group-{m['id']}"}
                })
        elif video_requested:
            # Video type (or no type filter) → return Scenes
            if filter_favorites and FAVORITE_TAG:
                fav_tag_id = _get_or_create_tag(FAVORITE_TAG)
                if fav_tag_id:
                    count_q = """query CountFavScenes($tid: [ID!]) {
                        findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
                    }"""
                    count_res = stash_query(count_q, {"tid": [fav_tag_id]})
                    total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
                    page = (start_index // limit) + 1
                    q = f"""query FindFavScenes($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                        findScenes(
                            scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                            filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                        ) {{
                            scenes {{ {scene_fields} }}
                        }}
                    }}"""
                    res = stash_query(q, {"tid": [fav_tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Favorites query returned {len(scenes)} scenes (page {page}, total {total_count})")
                    for s in scenes:
                        items.append(format_jellyfin_item(s))
                else:
                    logger.warning(f"IsFavorite filter requested but could not resolve FAVORITE_TAG '{FAVORITE_TAG}'")
            elif filter_favorites and not FAVORITE_TAG:
                logger.debug("IsFavorite filter requested but FAVORITE_TAG not configured - returning empty")
                total_count = 0
            else:
                count_q = "query { findScenes { count } }"
                count_res = stash_query(count_q)
                total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
                page = (start_index // limit) + 1
                q = f"""query FindScenes($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                    findScenes(filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                        scenes {{ {scene_fields} }}
                    }}
                }}"""
                res = stash_query(q, {"page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                logger.debug(f"Global query returned {len(scenes)} scenes (page {page}, total {total_count})")
                for s in scenes:
                    items.append(format_jellyfin_item(s))

    # Log pagination info for debugging
    logger.debug(f"Items response: returning {len(items)} items, TotalRecordCount={total_count}, StartIndex={start_index}")
    if len(items) > 0 and total_count > start_index + len(items):
        logger.debug(f"More items available: next page would start at {start_index + len(items)}")

    response_data = {"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index}
    return JSONResponse(response_data)

async def endpoint_item_details(request):
    item_id = request.path_params.get("item_id")

    # Full scene fields for queries (include performer image_path for People images, captions for subtitles)
    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

    # Handle special folder IDs - return the folder ITSELF (not children)

    # Handle FILTERS folder details
    if item_id.startswith("filters-"):
        filter_mode = item_id.replace("filters-", "").upper()
        saved_filters = stash_get_saved_filters(filter_mode)
        filter_count = len(saved_filters)

        mode_names = {"SCENES": "Scenes", "PERFORMERS": "Performers", "STUDIOS": "Studios", "GROUPS": "Groups"}
        mode_name = mode_names.get(filter_mode, filter_mode.capitalize())

        return JSONResponse({
            "Name": "FILTERS",
            "SortName": "!!!FILTERS",
            "Id": item_id,
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": filter_count,
            "RecursiveItemCount": filter_count,
            "Overview": f"Saved filters for {mode_name}",
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
        })

    # Handle individual saved filter details
    if item_id.startswith("filter-"):
        parts = item_id.split("-", 2)
        if len(parts) == 3:
            filter_mode = parts[1].upper()
            filter_id = parts[2]

            # Get the saved filter details
            query = """query FindSavedFilter($id: ID!) {
                findSavedFilter(id: $id) { id name mode }
            }"""
            res = stash_query(query, {"id": filter_id})
            saved_filter = res.get("data", {}).get("findSavedFilter")

            if saved_filter:
                filter_name = saved_filter.get("name", f"Filter {filter_id}")

                return JSONResponse({
                    "Name": filter_name,
                    "SortName": filter_name,
                    "Id": item_id,
                    "ServerId": SERVER_ID,
                    "Type": "BoxSet",
                    "CollectionType": "movies",
                    "IsFolder": True,
                    "ImageTags": {"Primary": "img"},
                    "ImageBlurHashes": {"Primary": {"img": "000000"}},
                    "PrimaryImageAspectRatio": 0.6667,
                    "BackdropImageTags": [],
                    "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
                })

    if item_id == "root-scenes":
        # Get actual count
        count_q = """query { findScenes { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        return JSONResponse({
            "Name": "All Scenes",
            "SortName": "All Scenes",
            "Id": "root-scenes",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-scenes"}
        })

    elif item_id == "root-studios":
        # Get actual count
        count_q = """query { findStudios { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

        return JSONResponse({
            "Name": "Studios",
            "SortName": "Studios",
            "Id": "root-studios",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-studios"}
        })

    elif item_id.startswith("studio-"):
        # Fetch actual studio info from Stash
        studio_id = item_id.replace("studio-", "")
        q = """query FindStudio($id: ID!) { findStudio(id: $id) { id name image_path scene_count } }"""
        res = stash_query(q, {"id": studio_id})
        studio = res.get("data", {}).get("findStudio", {})

        studio_name = studio.get("name", f"Studio {studio_id}")
        scene_count = studio.get("scene_count", 0)
        has_image = bool(studio.get("image_path"))

        return JSONResponse({
            "Name": studio_name,
            "SortName": studio_name,
            "Id": item_id,
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
        })

    elif item_id == "root-performers":
        # Get actual count
        count_q = """query { findPerformers { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

        return JSONResponse({
            "Name": "Performers",
            "SortName": "Performers",
            "Id": "root-performers",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-performers"}
        })

    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        # Fetch actual performer info from Stash (handle both performer- and person- prefixes)
        # Handle formats: performer-302, person-302, person-performer-302
        if item_id.startswith("person-performer-"):
            performer_id = item_id.replace("person-performer-", "")
        elif item_id.startswith("performer-"):
            performer_id = item_id.replace("performer-", "")
        else:
            performer_id = item_id.replace("person-", "")
        q = """query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path scene_count favorite } }"""
        res = stash_query(q, {"id": performer_id})
        performer = res.get("data", {}).get("findPerformer")

        if not performer:
            logger.warning(f"Performer not found: {performer_id}")
            return JSONResponse({"Items": [], "TotalRecordCount": 0}, status_code=404)

        performer_name = performer.get("name", f"Performer {performer_id}")
        scene_count = performer.get("scene_count", 0)
        has_image = bool(performer.get("image_path"))

        return JSONResponse({
            "Name": performer_name,
            "SortName": performer_name,
            "Id": item_id,
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"} if has_image else {},
            "ImageBlurHashes": {"Primary": {"img": "000000"}} if has_image else {},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": bool(performer.get("favorite")), "Played": False, "Key": item_id}
        })

    elif item_id == "root-groups":
        # Get actual count
        count_q = """query { findMovies { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)

        return JSONResponse({
            "Name": "Groups",
            "SortName": "Groups",
            "Id": "root-groups",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-groups"}
        })

    elif item_id.startswith("group-"):
        # Fetch actual group/movie info from Stash
        group_id = item_id.replace("group-", "")
        q = """query FindMovie($id: ID!) { findMovie(id: $id) { id name front_image_path scene_count tags { name } } }"""
        res = stash_query(q, {"id": group_id})
        group = res.get("data", {}).get("findMovie", {})

        group_name = group.get("name", f"Group {group_id}")
        scene_count = group.get("scene_count", 0)
        has_image = bool(group.get("front_image_path"))

        return JSONResponse({
            "Name": group_name,
            "SortName": group_name,
            "Id": item_id,
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": _is_group_favorite(group), "Played": False, "Key": item_id}
        })

    elif item_id == "root-tags":
        # Tags folder details
        # Count is Favorites + (All Tags if enabled) + saved filters count
        count = 1  # Favorites
        if ENABLE_ALL_TAGS:
            count += 1
        saved_filters = stash_get_saved_filters("TAGS")
        count += len(saved_filters)

        return JSONResponse({
            "Name": "Tags",
            "SortName": "Tags",
            "Id": "root-tags",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "ChildCount": count,
            "RecursiveItemCount": count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-tags"}
        })

    elif item_id == "tags-favorites":
        # Favorites subfolder details
        count_q = """query { findTags(tag_filter: {favorite: true}) { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findTags", {}).get("count", 0)

        return JSONResponse({
            "Name": "Favorites",
            "SortName": "!1-Favorites",
            "Id": "tags-favorites",
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-favorites"}
        })

    elif item_id == "tags-all":
        # All Tags subfolder details
        count_q = """query { findTags { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findTags", {}).get("count", 0)

        return JSONResponse({
            "Name": "All Tags",
            "SortName": "!2-All Tags",
            "Id": "tags-all",
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-all"}
        })

    elif item_id.startswith("tagitem-"):
        # Individual tag details
        tag_id = item_id.replace("tagitem-", "")
        q = """query FindTag($id: ID!) { findTag(id: $id) { id name scene_count image_path favorite } }"""
        res = stash_query(q, {"id": tag_id})
        tag = res.get("data", {}).get("findTag")

        if not tag:
            logger.warning(f"Tag not found: {tag_id}")
            return JSONResponse({"error": "Tag not found"}, status_code=404)

        tag_name = tag.get("name", f"Tag {tag_id}")
        scene_count = tag.get("scene_count", 0)
        has_image = bool(tag.get("image_path"))

        return JSONResponse({
            "Name": tag_name,
            "SortName": tag_name,
            "Id": item_id,
            "ServerId": SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": tag.get("favorite", False), "Played": False, "Key": item_id}
        })

    elif item_id.startswith("tag-"):
        # Tag-based folder (from TAG_GROUPS config)
        tag_slug = item_id[4:]  # Remove "tag-" prefix

        # Find the matching tag name from TAG_GROUPS config
        tag_name = None
        for t in TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break

        if tag_name:
            # Find tag ID and get scene count
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) {
                    tags { id name scene_count }
                }
            }"""
            tag_res = stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])

            # Find exact match
            tag_data = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_data = t
                    break

            scene_count = tag_data.get("scene_count", 0) if tag_data else 0

            return JSONResponse({
                "Name": tag_name,
                "SortName": tag_name,
                "Id": item_id,
                "ServerId": SERVER_ID,
                "Type": "CollectionFolder",
                "CollectionType": "movies",
                "IsFolder": True,
                "ImageTags": {"Primary": "icon"},
                "BackdropImageTags": [],
                "ChildCount": scene_count,
                "RecursiveItemCount": scene_count,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
            })
        else:
            logger.warning(f"Tag slug '{tag_slug}' not found in TAG_GROUPS config")
            return JSONResponse({"error": "Tag not found"}, status_code=404)

    elif item_id in ("Resume", "Latest"):
        # Return empty for resume/latest
        return JSONResponse({"Items": [], "TotalRecordCount": 0})

    # Otherwise it's a scene ID (scene-123 format) - extract numeric for Stash query
    if item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
    else:
        numeric_id = extract_numeric_id(item_id)

    q = f"""query FindScene($id: ID!) {{ findScene(id: $id) {{ {scene_fields} }} }}"""
    res = stash_query(q, {"id": numeric_id})
    scene = res.get("data", {}).get("findScene")
    if not scene:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(format_jellyfin_item(scene))

async def endpoint_sessions(request):
    """Handle session management endpoints (Playing, Progress, Stopped)."""
    path = request.url.path

    try:
        body = await request.json()
    except:
        body = {}

    item_id = body.get("ItemId", "")
    position_ticks = body.get("PositionTicks", 0)
    position_seconds = position_ticks / 10000000.0 if position_ticks else 0

    if "/Progress" in path and item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        try:
            q = """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"""
            stash_query(q, {"id": numeric_id, "resume_time": position_seconds})
        except Exception as e:
            logger.debug(f"Error saving resume position for {item_id}: {e}")

    elif "/Stopped" in path:
        if item_id.startswith("scene-"):
            numeric_id = item_id.replace("scene-", "")
            try:
                duration_ticks = body.get("RunTimeTicks") or body.get("NowPlayingItem", {}).get("RunTimeTicks", 0)
                duration_seconds = duration_ticks / 10000000.0 if duration_ticks else 0

                if duration_seconds <= 0:
                    try:
                        dq = f"""query FindScene($id: ID!) {{ findScene(id: $id) {{ files {{ duration }} }} }}"""
                        dres = stash_query(dq, {"id": numeric_id})
                        dfiles = dres.get("data", {}).get("findScene", {}).get("files", [])
                        duration_seconds = float(dfiles[0].get("duration") or 0) if dfiles else 0
                        logger.debug(f"Looked up duration from Stash for {item_id}: {duration_seconds:.0f}s")
                    except Exception:
                        pass

                played_percentage = (position_seconds / duration_seconds * 100) if duration_seconds > 0 else 0

                if played_percentage > 90:
                    q = """mutation SceneAddPlay($id: ID!) { sceneAddPlay(id: $id) { count } }"""
                    stash_query(q, {"id": numeric_id})
                    uq = """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"""
                    stash_query(uq, {"id": numeric_id, "resume_time": 0})
                    logger.info(f"▶ Auto-marked played: {item_id} ({played_percentage:.0f}% watched)")
                else:
                    q = """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"""
                    stash_query(q, {"id": numeric_id, "resume_time": position_seconds})
                    logger.info(f"⏸ Saved resume position: {item_id} at {position_seconds:.0f}s ({played_percentage:.0f}%)")
            except Exception as e:
                logger.error(f"Error updating play status for {item_id}: {e}")

        if item_id in _active_streams:
            title = _active_streams[item_id]["title"]
            mark_stream_stopped(item_id, from_stop_notification=True)
            logger.info(f"⏹ Stream stopped: {title} ({item_id})")
        elif item_id.startswith("scene-"):
            title = get_scene_title(item_id)
            mark_stream_stopped(item_id, from_stop_notification=True)
            logger.info(f"⏹ Stream stopped: {title} ({item_id})")
        else:
            logger.info(f"⏹ Stream stopped: {item_id}")

    return JSONResponse({})

async def endpoint_playback_info(request):
    """Return playback info with subtitle streams for a scene."""
    item_id = request.path_params.get("item_id")

    if not item_id or not item_id.startswith("scene-"):
        # Generic fallback
        return JSONResponse({
            "MediaSources": [{
                "Id": "src1",
                "Protocol": "File",
                "MediaStreams": [],
                "SupportsDirectPlay": True,
                "SupportsTranscoding": False
            }],
            "PlaySessionId": "session-1"
        })

    numeric_id = item_id.replace("scene-", "")

    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            id
            title
            files { path basename duration size video_codec audio_codec width height frame_rate bit_rate }
            captions { language_code caption_type }
        }
    }
    """

    result = stash_query(query, {"id": numeric_id})
    scene_data = result.get("data", {}).get("findScene") if result else None
    if not scene_data:
        return JSONResponse({
            "MediaSources": [{
                "Id": item_id,
                "Protocol": "File",
                "MediaStreams": [],
                "SupportsDirectPlay": True,
                "SupportsTranscoding": False
            }],
            "PlaySessionId": "session-1"
        })

    scene = scene_data
    files = scene.get("files", [])
    file_data = files[0] if files else {}
    path = file_data.get("path", "")
    duration = float(file_data.get("duration") or 0)
    captions = scene.get("captions") or []

    video_codec = (file_data.get("video_codec") or "h264").lower()
    audio_codec = (file_data.get("audio_codec") or "").lower()
    vid_width = file_data.get("width") or 0
    vid_height = file_data.get("height") or 0
    frame_rate = file_data.get("frame_rate") or 0
    bit_rate = file_data.get("bit_rate") or 0
    file_size = file_data.get("size") or 0

    container = "mp4"
    if path:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in ("mkv", "avi", "wmv", "flv", "webm", "mov", "ts", "m4v", "mp4"):
            container = ext

    video_stream = {
        "Index": 0,
        "Type": "Video",
        "Codec": video_codec,
        "IsDefault": True,
        "IsForced": False,
        "IsExternal": False,
    }
    if vid_width and vid_height:
        video_stream["Width"] = vid_width
        video_stream["Height"] = vid_height
        video_stream["AspectRatio"] = f"{vid_width}:{vid_height}"
    if bit_rate:
        video_stream["BitRate"] = bit_rate
    if frame_rate:
        video_stream["RealFrameRate"] = frame_rate
        video_stream["AverageFrameRate"] = frame_rate

    media_streams = [video_stream]

    audio_stream_idx = 1
    effective_audio_codec = audio_codec if audio_codec else "aac"
    media_streams.append({
        "Index": audio_stream_idx,
        "Type": "Audio",
        "Codec": effective_audio_codec,
        "Language": "und",
        "DisplayLanguage": "Unknown",
        "IsDefault": True,
        "IsForced": False,
        "IsExternal": False,
        "IsInterlaced": False,
        "IsTextSubtitleStream": False,
        "SupportsExternalStream": False,
        "DisplayTitle": f"{effective_audio_codec.upper()} - Stereo",
        "Channels": 2,
        "ChannelLayout": "stereo",
        "SampleRate": 48000,
    })
    audio_stream_idx += 1

    for idx, caption in enumerate(captions):
        lang_code = caption.get("language_code", "und")
        caption_type = (caption.get("caption_type", "") or "").lower()

        if caption_type not in ("srt", "vtt"):
            caption_type = "vtt"

        codec = "srt" if caption_type == "srt" else "webvtt"

        lang_names = {
            "en": "English", "de": "German", "es": "Spanish",
            "fr": "French", "it": "Italian", "nl": "Dutch",
            "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
            "zh": "Chinese", "ru": "Russian", "und": "Unknown"
        }
        display_lang = lang_names.get(lang_code, lang_code.upper())

        media_streams.append({
            "Index": audio_stream_idx + idx,
            "Type": "Subtitle",
            "Codec": codec,
            "Language": lang_code,
            "DisplayLanguage": display_lang,
            "DisplayTitle": f"{display_lang} ({caption_type.upper()})",
            "Title": display_lang,
            "IsDefault": idx == 0,
            "IsForced": False,
            "IsExternal": True,
            "IsTextSubtitleStream": True,
            "SupportsExternalStream": True,
            "DeliveryMethod": "External",
            "DeliveryUrl": f"Subtitles/{idx + 1}/0/Stream.{caption_type}"
        })

    logger.debug(f"PlaybackInfo for {item_id}: {len(captions)} subtitles")

    runtime_ticks = int(duration * 10000000) if duration else 0

    media_source = {
        "Id": item_id,
        "Name": scene.get("title") or os.path.basename(path),
        "Path": path,
        "Protocol": "File",
        "Type": "Default",
        "Container": container,
        "RunTimeTicks": runtime_ticks,
        "Size": int(file_size) if file_size else 0,
        "Bitrate": bit_rate if bit_rate else 0,
        "SupportsDirectPlay": True,
        "SupportsDirectStream": True,
        "SupportsTranscoding": False,
        "MediaStreams": media_streams,
        "DefaultAudioStreamIndex": 1,
        "DefaultSubtitleStreamIndex": -1,
    }

    return JSONResponse({
        "MediaSources": [media_source],
        "PlaySessionId": f"session-{item_id}"
    })

# get_numeric_id now lives in proxy/util/ids.py and is imported at the top.

def fetch_from_stash(url: str, extra_headers: Dict[str, str] = None, timeout: int = 30, stream: bool = False) -> Tuple[bytes, str, Dict[str, str]]:
    """
    Fetch content from Stash using authenticated session for proper redirect handling.
    Returns (data, content_type, response_headers).
    """
    # Use the authenticated session
    session = get_stash_session()

    # Add extra headers
    headers = extra_headers or {}

    try:
        response = session.get(url, headers=headers, timeout=timeout, stream=stream, allow_redirects=True)

        # Log response details for debugging
        content_type = response.headers.get('Content-Type', 'application/octet-stream')

        # Check if we got HTML instead of media (indicates auth failure)
        if 'text/html' in content_type:
            # Read a bit of content for debugging
            if stream:
                preview = next(response.iter_content(chunk_size=200), b'').decode('utf-8', errors='ignore')
            else:
                preview = response.text[:200]
            logger.error(f"Got HTML response instead of media from {url}")
            logger.error(f"First 200 chars: {preview}")
            raise Exception(f"Authentication failed - received HTML instead of media")

        response.raise_for_status()

        # Build response headers dict
        resp_headers = dict(response.headers)

        if stream:
            # For streaming, return chunks
            data = b''.join(response.iter_content(chunk_size=65536))
        else:
            data = response.content

        logger.debug(f"Fetch success from {url}: {len(data)} bytes, type={content_type}")
        return data, content_type, resp_headers

    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed for {url}: {e}")
        raise

async def endpoint_stream(request):
    """Proxy video stream from Stash with proper authentication using true streaming."""
    from starlette.responses import StreamingResponse

    item_id = request.path_params.get("item_id")
    numeric_id = get_numeric_id(item_id)
    stash_stream_url = f"{STASH_URL}/scene/{numeric_id}/stream"

    logger.debug(f"Proxying stream for {item_id} from {stash_stream_url}")

    # Build extra headers (forward Range header for seeking)
    extra_headers = {}
    if "range" in request.headers:
        extra_headers["Range"] = request.headers["range"]

    try:
        # Use authenticated session with stream=True for chunked transfer
        session = get_stash_session()
        response = session.get(stash_stream_url, headers=extra_headers, timeout=30, stream=True, allow_redirects=True)

        content_type = response.headers.get('Content-Type', 'video/mp4')

        # Check for auth failure (HTML instead of video)
        if 'text/html' in content_type:
            logger.error(f"Got HTML response instead of video from {stash_stream_url}")
            return JSONResponse({"error": "Authentication failed"}, status_code=401)

        response.raise_for_status()

        # Build response headers
        headers = {"Accept-Ranges": "bytes"}
        # Only include Content-Length for range requests (206) - needed for seeking
        # For full requests (200), omit Content-Length to use chunked transfer
        status_code = 206 if "Content-Range" in response.headers else 200
        if status_code == 206:
            if "Content-Length" in response.headers:
                headers["Content-Length"] = response.headers["Content-Length"]
            if "Content-Range" in response.headers:
                headers["Content-Range"] = response.headers["Content-Range"]

        content_length = response.headers.get("Content-Length", "?")
        logger.debug(f"Stream response: {content_length} bytes, type={content_type}, status={status_code}")

        # Async generator that yields chunks from Stash directly to client
        async def stream_generator():
            try:
                for chunk in response.iter_content(chunk_size=262144):  # 256KB chunks
                    if chunk:
                        yield chunk
            except GeneratorExit:
                # Client disconnected mid-stream (normal for video seeking)
                pass
            except Exception:
                # Any other error during streaming
                pass
            finally:
                response.close()

        return StreamingResponse(
            stream_generator(),
            media_type=content_type,
            headers=headers,
            status_code=status_code
        )

    except requests.exceptions.Timeout:
        logger.error(f"Stream timeout connecting to Stash: {stash_stream_url}")
        return JSONResponse({"error": "Stash timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"Stream proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

async def endpoint_download(request):
    """Handle download requests - proxy the video file from Stash with Content-Disposition."""
    from starlette.responses import StreamingResponse

    item_id = request.path_params.get("item_id")
    numeric_id = get_numeric_id(item_id)
    stash_stream_url = f"{STASH_URL}/scene/{numeric_id}/stream"

    logger.info(f"Download requested for {item_id}")

    try:
        # Get scene title for filename
        q = """query FindScene($id: ID!) {
            findScene(id: $id) { title files { path } }
        }"""
        res = stash_query(q, {"id": numeric_id})
        scene = res.get("data", {}).get("findScene", {})
        title = scene.get("title") or ""
        files = scene.get("files") or []
        if files:
            import os as _os
            original_filename = _os.path.basename(files[0].get("path", ""))
        else:
            original_filename = f"{title or item_id}.mp4"

        session = get_stash_session()
        response = session.get(stash_stream_url, timeout=30, stream=True, allow_redirects=True)

        content_type = response.headers.get('Content-Type', 'video/mp4')

        if 'text/html' in content_type:
            logger.error(f"Got HTML response instead of video for download {stash_stream_url}")
            return JSONResponse({"error": "Authentication failed"}, status_code=401)

        response.raise_for_status()

        headers = {}
        if "Content-Length" in response.headers:
            headers["Content-Length"] = response.headers["Content-Length"]
        headers["Content-Disposition"] = f'attachment; filename="{original_filename}"'

        async def stream_generator():
            try:
                for chunk in response.iter_content(chunk_size=262144):
                    if chunk:
                        yield chunk
            except GeneratorExit:
                pass
            except Exception:
                pass
            finally:
                response.close()

        return StreamingResponse(
            stream_generator(),
            media_type=content_type,
            headers=headers,
            status_code=200
        )

    except requests.exceptions.Timeout:
        logger.error(f"Download timeout connecting to Stash: {stash_stream_url}")
        return JSONResponse({"error": "Stash timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"Download proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

async def endpoint_subtitle(request):
    """Proxy subtitle/caption file from Stash."""
    item_id = request.path_params.get("item_id")
    subtitle_index = int(request.path_params.get("subtitle_index", 1))

    # Get the scene's numeric ID
    numeric_id = get_numeric_id(item_id)

    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            files { audio_codec }
            captions {
                language_code
                caption_type
            }
        }
    }
    """

    try:
        result = stash_query(query, {"id": numeric_id})
        scene_data = result.get("data", {}).get("findScene") if result else None
        if not scene_data:
            logger.error(f"Could not find scene {numeric_id} for subtitles")
            return JSONResponse({"error": "Scene not found"}, status_code=404)

        captions = scene_data.get("captions") or []
        if not captions:
            logger.warning(f"No captions found for scene {numeric_id}")
            return JSONResponse({"error": "No subtitles"}, status_code=404)

        # Calculate stream offset: video (always index 0) + audio (index 1 if present)
        # Infuse uses the MediaStreams Index value, not the DeliveryUrl
        files = scene_data.get("files", [])
        has_audio = bool(files and (files[0].get("audio_codec") or ""))
        stream_offset = 2 if has_audio else 1  # video + audio, or just video

        # Try stream-index-based mapping first (subtitle_index is the MediaStreams Index)
        caption_idx = subtitle_index - stream_offset
        # Fall back to 1-based caption index if stream offset doesn't work
        if caption_idx < 0 or caption_idx >= len(captions):
            caption_idx = subtitle_index - 1
        if caption_idx < 0 or caption_idx >= len(captions):
            logger.warning(f"Subtitle index {subtitle_index} out of range for scene {numeric_id}")
            return JSONResponse({"error": "Subtitle not found"}, status_code=404)

        caption = captions[caption_idx]
        caption_type = (caption.get("caption_type", "") or "").lower()

        # Normalize caption_type to srt or vtt (default to vtt if unknown)
        if caption_type not in ("srt", "vtt"):
            caption_type = "vtt"

        # Stash serves captions at /scene/{id}/caption?lang={lang}&type={type}
        lang_code = caption.get("language_code", "en") or "en"
        stash_caption_url = f"{STASH_URL}/scene/{numeric_id}/caption?lang={lang_code}&type={caption_type}"

        logger.debug(f"Proxying subtitle for {item_id} index {subtitle_index} from {stash_caption_url}")

        # Fetch the caption file
        image_headers = {"ApiKey": STASH_API_KEY} if STASH_API_KEY else {}
        data, content_type, _ = fetch_from_stash(stash_caption_url, extra_headers=image_headers, timeout=30)

        # Set appropriate content type for subtitle format
        if caption_type == "srt":
            content_type = "application/x-subrip"
        elif caption_type == "vtt":
            content_type = "text/vtt"
        else:
            content_type = "text/plain"

        logger.debug(f"Subtitle response: {len(data)} bytes, type={content_type}")
        from starlette.responses import Response
        return Response(content=data, media_type=content_type, headers={
            "Content-Disposition": f'attachment; filename="subtitle.{caption_type}"'
        })

    except Exception as e:
        logger.error(f"Subtitle proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

# generate_text_icon / _menu_icon / _filter_icon / _placeholder_icon all
# live in proxy/util/images.py (imported at top).

async def endpoint_image(request):
    """Proxy image from Stash with proper authentication. Handles scenes, studios, performers, groups, and menu icons."""
    global IMAGE_CACHE

    item_id = request.path_params.get("item_id")

    # Cache headers - short cache for generated icons to allow refresh
    icon_cache_headers = {"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"}

    # Handle menu icons for root folders
    if item_id in MENU_ICONS:
        # Generate PNG icon using Pillow drawing
        img_data, content_type = generate_menu_icon(item_id)
        logger.debug(f"Serving menu icon for {item_id}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Handle tag folder icons - use the actual tag name from config
    if item_id.startswith("tag-"):
        tag_slug = item_id[4:]  # Remove "tag-" prefix
        # Find the matching tag name from TAG_GROUPS config
        tag_name = None
        for t in TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break
        # Use the tag name or fall back to the slug
        display_name = tag_name if tag_name else tag_slug.replace('-', ' ').title()
        img_data, content_type = generate_text_icon(display_name)
        logger.debug(f"Serving text icon for tag folder: {display_name}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Handle genre icons - fetch the Stash tag image or generate a text icon
    if item_id.startswith("genre-"):
        tag_id = item_id[6:]  # Remove "genre-" prefix
        tag_img_url = f"{STASH_URL}/tag/{tag_id}/image"
        try:
            data, content_type, _ = fetch_from_stash(tag_img_url, timeout=10)
            is_svg = content_type == "image/svg+xml"
            is_gif = content_type == "image/gif"
            is_tiny = data and len(data) < 500
            if data and len(data) > 100 and not is_svg and not is_gif and not is_tiny:
                logger.debug(f"Serving Stash image for genre {tag_id}")
                from starlette.responses import Response
                return Response(content=data, media_type=content_type, headers=icon_cache_headers)
        except Exception:
            pass
        # No usable image — generate a text icon using the tag name from Stash
        try:
            tag_q = """query FindTag($id: ID!) { findTag(id: $id) { name } }"""
            tag_res = stash_query(tag_q, {"id": tag_id})
            tag_name = tag_res.get("data", {}).get("findTag", {}).get("name", tag_id)
        except Exception:
            tag_name = tag_id
        img_data, content_type = generate_text_icon(tag_name)
        logger.debug(f"Serving text icon for genre {tag_id}: {tag_name}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Handle FILTERS folder icons
    if item_id.startswith("filters-"):
        img_data, content_type = generate_filter_icon("FILTERS")
        logger.debug(f"Serving text icon for filters folder: {item_id}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Handle individual saved filter icons
    if item_id.startswith("filter-"):
        # Format: filter-{mode}-{filter_id}
        parts = item_id.split("-", 2)
        if len(parts) == 3:
            filter_id = parts[2]
            # Get the filter name from Stash
            query = """query FindSavedFilter($id: ID!) {
                findSavedFilter(id: $id) { name }
            }"""
            res = stash_query(query, {"id": filter_id})
            saved_filter = res.get("data", {}).get("findSavedFilter")
            filter_name = saved_filter.get("name", f"Filter {filter_id}") if saved_filter else f"Filter {filter_id}"
            img_data, content_type = generate_filter_icon(filter_name)
            logger.debug(f"Serving text icon for saved filter: {filter_name}")
            from starlette.responses import Response
            return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Handle Tags subfolder icons (tags-favorites, tags-all)
    if item_id == "tags-favorites":
        img_data, content_type = generate_filter_icon("Favorites")
        logger.debug(f"Serving text icon for tags-favorites")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    if item_id == "tags-all":
        img_data, content_type = generate_filter_icon("All Tags")
        logger.debug(f"Serving text icon for tags-all")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Handle individual tag images (tagitem-{id}) - fetch from Stash or generate text icon
    if item_id.startswith("tagitem-"):
        tag_id = item_id.replace("tagitem-", "")
        # First check if tag has an image in Stash
        q = """query FindTag($id: ID!) { findTag(id: $id) { name image_path } }"""
        res = stash_query(q, {"id": tag_id})
        tag = res.get("data", {}).get("findTag")
        if tag:
            tag_name = tag.get("name", f"Tag {tag_id}")
            if tag.get("image_path"):
                # Fetch the tag image from Stash
                tag_img_url = f"{STASH_URL}/tag/{tag_id}/image"
                image_headers = {"ApiKey": STASH_API_KEY} if STASH_API_KEY else {}
                try:
                    data, content_type, _ = fetch_from_stash(tag_img_url, extra_headers=image_headers, timeout=30)
                    # Check for valid image data:
                    # - Reject SVG (Infuse doesn't support SVG)
                    # - Reject GIF (often transparent placeholders that appear as black boxes)
                    # - Reject tiny images (<500 bytes, likely 1x1 placeholders)
                    is_svg = content_type == "image/svg+xml"
                    is_gif = content_type == "image/gif"
                    is_tiny = data and len(data) < 500

                    if data and len(data) > 100 and not is_svg and not is_gif and not is_tiny:
                        logger.debug(f"Serving Stash image for tag '{tag_name}': {len(data)} bytes, {content_type}")
                        from starlette.responses import Response
                        return Response(content=data, media_type=content_type, headers=icon_cache_headers)
                    elif is_svg:
                        logger.debug(f"Tag '{tag_name}' has SVG placeholder, generating PNG text icon instead")
                    elif is_gif:
                        logger.debug(f"Tag '{tag_name}' has GIF (often transparent), generating PNG text icon instead")
                    elif is_tiny:
                        logger.debug(f"Tag '{tag_name}' has tiny image ({len(data)} bytes), generating PNG text icon instead")
                except Exception as e:
                    logger.debug(f"Failed to fetch tag image for '{tag_name}', using text icon: {e}")
            # No image, SVG placeholder, or fetch failed - generate text icon with tag name
            img_data, content_type = generate_filter_icon(tag_name)
            logger.debug(f"Serving text icon for tag: {tag_name}")
            from starlette.responses import Response
            return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)
        else:
            # Tag not found - generate generic fallback icon
            img_data, content_type = generate_filter_icon(f"Tag {tag_id}")
            logger.debug(f"Tag not found, serving fallback icon for: {tag_id}")
            from starlette.responses import Response
            return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Check query params for placeholder flag (set when group has no front_image)
    image_tag = request.query_params.get("tag", "")
    if image_tag == "placeholder" and item_id.startswith("group-"):
        # Generate placeholder icon for groups without images
        img_data, content_type = generate_placeholder_icon("group")
        logger.debug(f"Serving placeholder icon for {item_id}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers=icon_cache_headers)

    # Determine image URL and whether to resize based on item type
    needs_portrait_resize = False
    is_group_image = False  # Flag to enable SVG placeholder detection for groups
    if item_id.startswith("studio-"):
        numeric_id = item_id.replace("studio-", "")
        stash_img_url = f"{STASH_URL}/studio/{numeric_id}/image"
        needs_portrait_resize = True  # Studio logos need portrait padding for Infuse tiles
    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        # Handle formats: performer-302, person-302, person-performer-302
        if item_id.startswith("person-performer-"):
            numeric_id = item_id.replace("person-performer-", "")
        elif item_id.startswith("performer-"):
            numeric_id = item_id.replace("performer-", "")
        else:
            numeric_id = item_id.replace("person-", "")
        stash_img_url = f"{STASH_URL}/performer/{numeric_id}/image"
        # Performer images are usually already portrait/square
    elif item_id.startswith("group-"):
        numeric_id = item_id.replace("group-", "")
        # Correct endpoint is /group/{id}/frontimage with cache-busting timestamp
        import time
        cache_bust = int(time.time())
        stash_img_url = f"{STASH_URL}/group/{numeric_id}/frontimage?t={cache_bust}"
        # Group images are usually movie posters (portrait)
        # We'll check for Stash's SVG placeholder after fetch and fallback to GraphQL if needed
        is_group_image = True
    elif item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        stash_img_url = f"{STASH_URL}/scene/{numeric_id}/screenshot"
    else:
        # Fallback - try as scene
        numeric_id = get_numeric_id(item_id)
        stash_img_url = f"{STASH_URL}/scene/{numeric_id}/screenshot"

    logger.debug(f"Proxying image for {item_id} from {stash_img_url}")

    # Cache control headers - disable caching for now to force refresh
    cache_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    # Check cache for resized images
    cache_key = (item_id, "portrait" if needs_portrait_resize else "original")
    if cache_key in IMAGE_CACHE:
        cached_data, cached_type = IMAGE_CACHE[cache_key]
        logger.debug(f"Cache hit for {item_id}")
        from starlette.responses import Response
        return Response(content=cached_data, media_type=cached_type, headers=cache_headers)

    # Explicitly pass ApiKey header for image requests (required for Stash image endpoints)
    image_headers = {"ApiKey": STASH_API_KEY} if STASH_API_KEY else {}

    def _name_text_icon(item_id, numeric_id):
        """Query Stash for the item's name and return a text icon. Used as fallback when no image."""
        name = None
        try:
            if item_id.startswith("performer-") or item_id.startswith("person-"):
                res = stash_query("query($id: ID!) { findPerformer(id: $id) { name } }", {"id": numeric_id})
                name = (res.get("data", {}).get("findPerformer") or {}).get("name")
            elif item_id.startswith("studio-"):
                res = stash_query("query($id: ID!) { findStudio(id: $id) { name } }", {"id": numeric_id})
                name = (res.get("data", {}).get("findStudio") or {}).get("name")
            elif item_id.startswith("scene-"):
                res = stash_query("query($id: ID!) { findScene(id: $id) { title } }", {"id": numeric_id})
                name = (res.get("data", {}).get("findScene") or {}).get("title")
        except Exception:
            pass
        img_data, ct = generate_text_icon(name or item_id)
        IMAGE_CACHE[cache_key] = (img_data, ct)
        return img_data, ct

    try:
        data, content_type, _ = fetch_from_stash(stash_img_url, extra_headers=image_headers, timeout=30)

        # For performers, studios, scenes: generate a text icon if response is missing/invalid/SVG
        if item_id.startswith(("performer-", "person-", "studio-", "scene-")):
            is_invalid = (
                not data or len(data) < 500 or
                (content_type and not content_type.startswith("image/")) or
                content_type == "image/svg+xml"
            )
            if is_invalid:
                logger.debug(f"No valid image for {item_id}, generating text icon")
                img_data, ct = _name_text_icon(item_id, numeric_id)
                from starlette.responses import Response
                return Response(content=img_data, media_type=ct, headers=cache_headers)

        # Check for empty or invalid response (groups with no artwork)
        if not data or len(data) < 100:
            # Response too small to be a valid image
            if item_id.startswith("group-"):
                logger.debug(f"Empty/small response for group image, using placeholder: {item_id}")
                img_data, ct = generate_placeholder_icon("group")
                from starlette.responses import Response
                return Response(content=img_data, media_type=ct, headers=cache_headers)

        # Check if we got an image content type
        if content_type and not content_type.startswith("image/"):
            if item_id.startswith("group-"):
                logger.debug(f"Non-image response for group ({content_type}), using placeholder: {item_id}")
                img_data, ct = generate_placeholder_icon("group")
                from starlette.responses import Response
                return Response(content=img_data, media_type=ct, headers=cache_headers)

        # Detect Stash's SVG placeholder for groups (usually ~1.4KB SVG)
        # If we get SVG when we expect an image, try GraphQL fallback
        if is_group_image and content_type == "image/svg+xml":
            logger.warning(f"Got SVG placeholder for {item_id}, trying GraphQL fallback")
            # Try to fetch the front_image via GraphQL
            query = """
            query FindGroup($id: ID!) {
                findGroup(id: $id) {
                    front_image_path
                }
            }
            """
            try:
                gql_result = stash_query(query, {"id": numeric_id})
                gql_data = gql_result.get("data", {}).get("findGroup") if gql_result else None
                if gql_data:
                    front_image_path = gql_data.get("front_image_path")
                    if front_image_path:
                        # Fetch the image using the path from GraphQL
                        import time as time_module
                        gql_img_url = f"{STASH_URL}{front_image_path}?t={int(time_module.time())}"
                        logger.debug(f"GraphQL fallback: fetching from {gql_img_url}")
                        data, content_type, _ = fetch_from_stash(gql_img_url, extra_headers=image_headers, timeout=30)
                        if data and len(data) > 1000 and content_type != "image/svg+xml":
                            logger.debug(f"GraphQL fallback successful: {len(data)} bytes, type={content_type}")
                        else:
                            logger.warning(f"GraphQL fallback still returned placeholder/SVG")
                            img_data, ct = generate_placeholder_icon("group")
                            from starlette.responses import Response
                            return Response(content=img_data, media_type=ct, headers=cache_headers)
                    else:
                        logger.warning(f"No front_image_path in GraphQL response for {item_id}")
                        img_data, ct = generate_placeholder_icon("group")
                        from starlette.responses import Response
                        return Response(content=img_data, media_type=ct, headers=cache_headers)
            except Exception as gql_err:
                logger.error(f"GraphQL fallback failed for {item_id}: {gql_err}")
                img_data, ct = generate_placeholder_icon("group")
                from starlette.responses import Response
                return Response(content=img_data, media_type=ct, headers=cache_headers)

        # Resize studio images to portrait 2:3 aspect ratio for Infuse tiles
        if needs_portrait_resize and ENABLE_IMAGE_RESIZE and PILLOW_AVAILABLE:
            data, content_type = pad_image_to_portrait(data, target_width=400, target_height=600)
            logger.debug(f"Resized studio image to 400x600 portrait (2:3)")

            # Cache the resized image
            if len(IMAGE_CACHE) >= IMAGE_CACHE_MAX_SIZE:
                # Remove oldest entry (simple FIFO)
                oldest_key = next(iter(IMAGE_CACHE))
                del IMAGE_CACHE[oldest_key]
            IMAGE_CACHE[cache_key] = (data, content_type)

        from starlette.responses import Response
        logger.debug(f"Image response: {len(data)} bytes, type={content_type}")
        return Response(content=data, media_type=content_type, headers=cache_headers)

    except Exception as e:
        logger.error(f"Image proxy error for {item_id}: {e}")
        from starlette.responses import Response

        if item_id.startswith("group-"):
            img_data, content_type = generate_placeholder_icon("group")
            return Response(content=img_data, media_type=content_type, headers=cache_headers)
        elif item_id.startswith(("performer-", "person-", "studio-", "scene-")):
            img_data, ct = _name_text_icon(item_id, numeric_id)
            return Response(content=img_data, media_type=ct, headers=cache_headers)

        return Response(content=PLACEHOLDER_PNG, media_type='image/png', headers=cache_headers)

async def endpoint_user_items_resume(request):
    """Return in-progress items — scenes with a resume position that are not
    effectively complete. Stash keeps `resume_time` set after full playback, so
    a raw `resume_time > 0` filter yields all historically-watched scenes. We
    exclude items past RESUME_COMPLETE_THRESHOLD of their duration and honor
    the client's Limit param so a home-screen "Continue Watching" row shows a
    sensible count."""
    RESUME_COMPLETE_THRESHOLD = 0.90  # >=90% watched is "finished", not resume
    try:
        limit = int(request.query_params.get("Limit", "24"))
    except (TypeError, ValueError):
        limit = 24
    limit = max(1, min(limit, 100))
    # Over-fetch so client-side filtering still yields up to `limit` items.
    fetch = min(limit * 3, 100)

    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"
    try:
        q = f"""query FindScenes {{
            findScenes(
                scene_filter: {{resume_time: {{value: 0, modifier: GREATER_THAN}}}},
                filter: {{per_page: {fetch}, sort: "last_played_at", direction: DESC}}
            ) {{
                count
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q)
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])

        def is_in_progress(scene):
            resume = scene.get("resume_time") or 0
            if resume <= 0:
                return False
            files = scene.get("files") or []
            duration = files[0].get("duration") if files else None
            if not duration or duration <= 0:
                return True  # unknown duration -> keep it, better than dropping it
            return resume < duration * RESUME_COMPLETE_THRESHOLD

        in_progress = [s for s in scenes if is_in_progress(s)][:limit]
        items = [format_jellyfin_item(s) for s in in_progress]
        return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error fetching resume items: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})



async def endpoint_items_counts(request):
    """Return item counts by type."""
    # Query Stash for counts
    try:
        count_q = """query {
            findScenes { count }
            findPerformers { count }
            findStudios { count }
            findMovies { count }
        }"""
        res = stash_query(count_q)
        data = res.get("data", {})
        return JSONResponse({
            "MovieCount": data.get("findScenes", {}).get("count", 0),
            "SeriesCount": 0,
            "EpisodeCount": 0,
            "ArtistCount": data.get("findPerformers", {}).get("count", 0),
            "ProgramCount": 0,
            "TrailerCount": 0,
            "SongCount": 0,
            "AlbumCount": 0,
            "MusicVideoCount": 0,
            "BoxSetCount": data.get("findMovies", {}).get("count", 0),
            "BookCount": 0,
            "ItemCount": data.get("findScenes", {}).get("count", 0)
        })
    except Exception as e:
        logger.error(f"Error getting item counts: {e}")
        return JSONResponse({"ItemCount": 0})

async def endpoint_user_favorites(request):
    """Return favorite items - scenes with the configured FAVORITE_TAG in Stash."""
    if not FAVORITE_TAG:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"
    try:
        tag_id = _get_or_create_tag(FAVORITE_TAG)
        if not tag_id:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
        q = f"""query FindScenes($tag_ids: [ID!]) {{
            findScenes(scene_filter: {{tags: {{value: $tag_ids, modifier: INCLUDES}}}}, filter: {{per_page: 100, sort: "updated_at", direction: DESC}}) {{
                count
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"tag_ids": [tag_id]})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        count = res.get("data", {}).get("findScenes", {}).get("count", 0)
        items = [format_jellyfin_item(s) for s in scenes]
        return JSONResponse({"Items": items, "TotalRecordCount": count, "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error fetching favorites: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_user_item_favorite(request):
    """Mark item as favorite in Stash. Scenes/groups use FAVORITE_TAG, performers use native favorite field."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        if not FAVORITE_TAG:
            logger.debug(f"Favorite toggled but FAVORITE_TAG not configured - ignoring")
            return JSONResponse({"IsFavorite": True, "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False, "Key": item_id, "ItemId": item_id})
        numeric_id = item_id.replace("scene-", "")
        try:
            tag_id = _get_or_create_tag(FAVORITE_TAG)
            if tag_id:
                scene_res = stash_query("""query FindScene($id: ID!) { findScene(id: $id) { id tags { id } } }""", {"id": numeric_id})
                scene = scene_res.get("data", {}).get("findScene") if scene_res else None
                if scene:
                    existing_tag_ids = [t["id"] for t in scene.get("tags", [])]
                    if tag_id not in existing_tag_ids:
                        existing_tag_ids.append(tag_id)
                    q = """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }"""
                    stash_query(q, {"input": {"id": numeric_id, "tag_ids": existing_tag_ids}})
                    logger.info(f"★ Favorited scene: {item_id} (added tag '{FAVORITE_TAG}')")
        except Exception as e:
            logger.error(f"Error favoriting {item_id}: {e}")
    elif item_id.startswith("group-"):
        if not FAVORITE_TAG:
            logger.debug(f"Favorite toggled on group but FAVORITE_TAG not configured - ignoring")
            return JSONResponse({"IsFavorite": True, "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False, "Key": item_id, "ItemId": item_id})
        group_id = item_id.replace("group-", "")
        try:
            tag_id = _get_or_create_tag(FAVORITE_TAG)
            if tag_id:
                group_res = stash_query("""query FindMovie($id: ID!) { findMovie(id: $id) { id tags { id } } }""", {"id": group_id})
                group = group_res.get("data", {}).get("findMovie") if group_res else None
                if group:
                    existing_tag_ids = [t["id"] for t in group.get("tags", [])]
                    if tag_id not in existing_tag_ids:
                        existing_tag_ids.append(tag_id)
                    q = """mutation MovieUpdate($input: MovieUpdateInput!) { movieUpdate(input: $input) { id } }"""
                    stash_query(q, {"input": {"id": group_id, "tag_ids": existing_tag_ids}})
                    logger.info(f"★ Favorited group: {item_id} (added tag '{FAVORITE_TAG}')")
        except Exception as e:
            logger.error(f"Error favoriting {item_id}: {e}")
    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        if item_id.startswith("person-performer-"):
            performer_id = item_id.replace("person-performer-", "")
        elif item_id.startswith("performer-"):
            performer_id = item_id.replace("performer-", "")
        else:
            performer_id = item_id.replace("person-", "")
        try:
            q = """mutation PerformerUpdate($input: PerformerUpdateInput!) { performerUpdate(input: $input) { id favorite } }"""
            stash_query(q, {"input": {"id": performer_id, "favorite": True}})
            logger.info(f"★ Favorited performer: {item_id}")
        except Exception as e:
            logger.error(f"Error favoriting performer {item_id}: {e}")
    elif item_id.startswith("studio-"):
        studio_id = item_id.replace("studio-", "")
        try:
            q = """mutation StudioUpdate($input: StudioUpdateInput!) { studioUpdate(input: $input) { id favorite } }"""
            stash_query(q, {"input": {"id": studio_id, "favorite": True}})
            logger.info(f"★ Favorited studio: {item_id}")
        except Exception as e:
            logger.error(f"Error favoriting studio {item_id}: {e}")
    return JSONResponse({"IsFavorite": True, "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False, "Key": item_id, "ItemId": item_id})

async def endpoint_user_item_unfavorite(request):
    """Remove favorite in Stash. Scenes/groups use FAVORITE_TAG, performers use native favorite field."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        if not FAVORITE_TAG:
            return JSONResponse({"IsFavorite": False, "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False, "Key": item_id, "ItemId": item_id})
        numeric_id = item_id.replace("scene-", "")
        try:
            tag_id = _get_or_create_tag(FAVORITE_TAG)
            if tag_id:
                scene_res = stash_query("""query FindScene($id: ID!) { findScene(id: $id) { id tags { id } } }""", {"id": numeric_id})
                scene = scene_res.get("data", {}).get("findScene") if scene_res else None
                if scene:
                    existing_tag_ids = [t["id"] for t in scene.get("tags", []) if t["id"] != tag_id]
                    q = """mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }"""
                    stash_query(q, {"input": {"id": numeric_id, "tag_ids": existing_tag_ids}})
                    logger.info(f"☆ Unfavorited scene: {item_id} (removed tag '{FAVORITE_TAG}')")
        except Exception as e:
            logger.error(f"Error unfavoriting {item_id}: {e}")
    elif item_id.startswith("group-"):
        if not FAVORITE_TAG:
            return JSONResponse({"IsFavorite": False, "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False, "Key": item_id, "ItemId": item_id})
        group_id = item_id.replace("group-", "")
        try:
            tag_id = _get_or_create_tag(FAVORITE_TAG)
            if tag_id:
                group_res = stash_query("""query FindMovie($id: ID!) { findMovie(id: $id) { id tags { id } } }""", {"id": group_id})
                group = group_res.get("data", {}).get("findMovie") if group_res else None
                if group:
                    existing_tag_ids = [t["id"] for t in group.get("tags", []) if t["id"] != tag_id]
                    q = """mutation MovieUpdate($input: MovieUpdateInput!) { movieUpdate(input: $input) { id } }"""
                    stash_query(q, {"input": {"id": group_id, "tag_ids": existing_tag_ids}})
                    logger.info(f"☆ Unfavorited group: {item_id} (removed tag '{FAVORITE_TAG}')")
        except Exception as e:
            logger.error(f"Error unfavoriting {item_id}: {e}")
    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        if item_id.startswith("person-performer-"):
            performer_id = item_id.replace("person-performer-", "")
        elif item_id.startswith("performer-"):
            performer_id = item_id.replace("performer-", "")
        else:
            performer_id = item_id.replace("person-", "")
        try:
            q = """mutation PerformerUpdate($input: PerformerUpdateInput!) { performerUpdate(input: $input) { id favorite } }"""
            stash_query(q, {"input": {"id": performer_id, "favorite": False}})
            logger.info(f"☆ Unfavorited performer: {item_id}")
        except Exception as e:
            logger.error(f"Error unfavoriting performer {item_id}: {e}")
    elif item_id.startswith("studio-"):
        studio_id = item_id.replace("studio-", "")
        try:
            q = """mutation StudioUpdate($input: StudioUpdateInput!) { studioUpdate(input: $input) { id favorite } }"""
            stash_query(q, {"input": {"id": studio_id, "favorite": False}})
            logger.info(f"☆ Unfavorited studio: {item_id}")
        except Exception as e:
            logger.error(f"Error unfavoriting studio {item_id}: {e}")
    return JSONResponse({"IsFavorite": False, "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False, "Key": item_id, "ItemId": item_id})

async def endpoint_items_filters(request):
    """Return filter options populated from Stash data."""
    parent_id = request.query_params.get("parentId") or request.query_params.get("ParentId")

    try:
        tags_q = """query { findTags(filter: {per_page: 200, sort: "name", direction: ASC}) { tags { name } } }"""
        studios_q = """query { findStudios(filter: {per_page: 200, sort: "name", direction: ASC}) { studios { name } } }"""
        tags_res = stash_query(tags_q)
        studios_res = stash_query(studios_q)

        tag_names = [t["name"] for t in tags_res.get("data", {}).get("findTags", {}).get("tags", [])]
        studio_names = [s["name"] for s in studios_res.get("data", {}).get("findStudios", {}).get("studios", [])]

        return JSONResponse({
            "Genres": studio_names,
            "Tags": tag_names,
            "OfficialRatings": [],
            "Years": [],
        })
    except Exception as e:
        logger.error(f"Failed to fetch filters: {e}")
        return JSONResponse({
            "Genres": [],
            "Tags": [],
            "OfficialRatings": [],
            "Years": [],
        })




async def endpoint_user_played_items(request):
    """Mark item as played by incrementing play count in Stash."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        try:
            q = """mutation SceneAddPlay($id: ID!) { sceneAddPlay(id: $id) { count } }"""
            result = stash_query(q, {"id": numeric_id})
            new_count = (result.get("data", {}).get("sceneAddPlay") or {}).get("count") if result else None
            if new_count is not None:
                logger.info(f"▶ Marked played: {item_id} (play count: {new_count})")
            else:
                logger.warning(f"Failed to mark played {item_id}: {result}")
        except Exception as e:
            logger.error(f"Error marking played {item_id}: {e}")
    return JSONResponse({"PlayCount": 1, "Played": True, "IsFavorite": False, "PlaybackPositionTicks": 0})

async def endpoint_user_unplayed_items(request):
    """Mark item as unplayed by resetting play count in Stash."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        try:
            dq = """mutation SceneDeletePlay($id: ID!) { sceneDeletePlay(id: $id) { count } }"""
            # sceneDeletePlay removes one play at a time — loop until history is empty
            for _ in range(1000):
                res = stash_query(dq, {"id": numeric_id})
                count = (res.get("data", {}).get("sceneDeletePlay") or {}).get("count") if res else 0
                if not count:
                    break
            aq = """mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"""
            stash_query(aq, {"id": numeric_id, "resume_time": 0})
            logger.info(f"⏮ Marked unplayed: {item_id}")
        except Exception as e:
            logger.error(f"Error marking unplayed {item_id}: {e}")
    return JSONResponse({"PlayCount": 0, "Played": False, "IsFavorite": False, "PlaybackPositionTicks": 0})



def _scene_filter_clause_for_parent(parent_id):
    """Return (gql_filter_clause, variables_dict) for a given parent_id context, or (None, None)."""
    if not parent_id:
        return None, None
    if parent_id.startswith("performer-"):
        pid = parent_id.replace("performer-", "")
        return "scene_filter: {performers: {value: $ids, modifier: INCLUDES}}", {"ids": [pid]}
    elif parent_id.startswith("studio-"):
        sid = parent_id.replace("studio-", "")
        return "scene_filter: {studios: {value: $ids, modifier: INCLUDES}}", {"ids": [sid]}
    elif parent_id.startswith("group-"):
        gid = parent_id.replace("group-", "")
        return "scene_filter: {movies: {value: $ids, modifier: INCLUDES}}", {"ids": [gid]}
    elif parent_id.startswith("tagitem-"):
        tid = parent_id.replace("tagitem-", "")
        return "scene_filter: {tags: {value: $ids, modifier: INCLUDES}}", {"ids": [tid]}
    return None, None


async def endpoint_genres(request):
    """Return genres (Stash tags), optionally filtered to only those present in a parent context."""
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")

    try:
        filter_clause, fvars = _scene_filter_clause_for_parent(parent_id)

        if filter_clause:
            # Fetch only the tags present in scenes matching this context
            q = f"""query FindSceneTags($ids: [ID!]) {{
                findScenes({filter_clause}, filter: {{per_page: -1}}) {{
                    scenes {{ tags {{ id name }} }}
                }}
            }}"""
            res = stash_query(q, fvars)
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            seen = {}
            for s in scenes:
                for t in s.get("tags", []):
                    seen[t["id"]] = t["name"]
            items = [
                {"Name": name, "Id": f"genre-{tid}", "ServerId": SERVER_ID,
                 "Type": "Genre", "ImageTags": {"Primary": "img"}, "ImageBlurHashes": {"Primary": {"img": "000000"}}, "BackdropImageTags": []}
                for tid, name in sorted(seen.items(), key=lambda x: x[1])
            ]
        else:
            # Return all tags with at least one scene
            q = """query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) {
                tags { id name scene_count }
            }}"""
            res = stash_query(q)
            tags = res.get("data", {}).get("findTags", {}).get("tags", [])
            items = [
                {"Name": t["name"], "Id": f"genre-{t['id']}", "ServerId": SERVER_ID,
                 "Type": "Genre", "ImageTags": {"Primary": "img"}, "ImageBlurHashes": {"Primary": {"img": "000000"}}, "BackdropImageTags": []}
                for t in tags if t.get("scene_count", 0) > 0
            ]

        return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error getting genres: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_persons(request):
    """Return persons - maps to Stash performers."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm")
    filters_param = request.query_params.get("Filters") or request.query_params.get("filters") or ""
    filter_favorites = "isfavorite" in filters_param.lower()
    folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

    try:
        page = (start_index // limit) + 1

        if search_term:
            clean_search = search_term.strip('"\'')
            logger.debug(f"Persons search: '{clean_search}'")

            count_q = """query CountPerformers($q: String!) {
                findPerformers(filter: {q: $q}) { count }
            }"""
            count_res = stash_query(count_q, {"q": clean_search})
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

            q = """query FindPerformers($q: String!, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(filter: {q: $q, page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = stash_query(q, {"q": clean_search, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            logger.debug(f"Persons search '{clean_search}' returned {total_count} matches")
        elif filter_favorites:
            # Return only performers marked as favorite in Stash (native favorite field)
            count_q = """query { findPerformers(performer_filter: {filter_favorites: true}) { count } }"""
            count_res = stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

            q = """query FindFavPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(
                    performer_filter: {filter_favorites: true},
                    filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                ) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            logger.debug(f"Persons favorites returned {total_count} favorite performers")
        else:
            count_q = """query { findPerformers { count } }"""
            count_res = stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

            q = """query FindPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})

        performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])

        items = []
        for p in performers:
            has_image = bool(p.get("image_path"))
            item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": SERVER_ID,
                "Type": "Person",
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "BackdropImageTags": []
            }
            items.append(item)
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting persons: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_studios(request):
    """Return studios, optionally filtered to only those present in a parent context."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

    try:
        filter_clause, fvars = _scene_filter_clause_for_parent(parent_id)

        if filter_clause:
            # Fetch only the studios present in scenes matching this context
            q = f"""query FindSceneStudios($ids: [ID!]) {{
                findScenes({filter_clause}, filter: {{per_page: -1}}) {{
                    scenes {{ studio {{ id name image_path }} }}
                }}
            }}"""
            res = stash_query(q, fvars)
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            seen = {}
            for s in scenes:
                studio = s.get("studio")
                if studio:
                    seen[studio["id"]] = studio
            items = [
                {"Name": s["name"], "Id": f"studio-{s['id']}", "ServerId": SERVER_ID,
                 "Type": "Studio",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for s in sorted(seen.values(), key=lambda x: x["name"])
            ]
            return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
        else:
            count_q = """query { findStudios { count } }"""
            count_res = stash_query(count_q)
            total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

            page = (start_index // limit) + 1
            q = """query FindStudios($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findStudios(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    studios { id name image_path scene_count }
                }
            }"""
            res = stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
            items = [
                {"Name": s["name"], "Id": f"studio-{s['id']}", "ServerId": SERVER_ID,
                 "Type": "Studio",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for s in studios
            ]
            return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting studios: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})



async def endpoint_search_hints(request):
    """Swiftfin search - returns SearchHints format used by /Search/Hints."""
    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm") or ""
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 20)
    limit = max(1, min(limit, 50))

    include_item_types_raw = [v for k, v in request.query_params.multi_items() if k.lower() == "includeitemtypes"]
    include_item_types = []
    for val in include_item_types_raw:
        include_item_types.extend([t.strip().lower() for t in val.split(",") if t.strip()])

    hints = []
    total_count = 0

    if not search_term.strip():
        return JSONResponse({"SearchHints": [], "TotalRecordCount": 0})

    clean_search = search_term.strip('"\'')

    search_scenes = not include_item_types or "movie" in include_item_types or "video" in include_item_types
    search_persons = not include_item_types or "person" in include_item_types

    try:
        if search_scenes:
            q = """query FindScenes($q: String!, $per_page: Int!) {
                findScenes(filter: {q: $q, per_page: $per_page, sort: "date", direction: DESC}) {
                    count
                    scenes { id title date files { duration } }
                }
            }"""
            res = stash_query(q, {"q": clean_search, "per_page": limit})
            data = res.get("data", {}).get("findScenes", {})
            total_count += data.get("count", 0)
            for s in data.get("scenes", []):
                scene_id = f"scene-{s['id']}"
                duration = 0
                if s.get("files"):
                    duration = s["files"][0].get("duration") or 0
                title = s.get("title") or f"Scene {s['id']}"
                hint = {
                    "Name": title,
                    "Id": scene_id,
                    "ServerId": SERVER_ID,
                    "Type": "Movie",
                    "MediaType": "Video",
                    "RunTimeTicks": int(duration * 10000000),
                    "PrimaryImageTag": "img",
                    "ImageTag": "img",
                }
                date = s.get("date")
                if date:
                    hint["ProductionYear"] = int(date[:4])
                hints.append(hint)

        if search_persons:
            perf_limit = max(5, limit // 2)
            q = """query FindPerformers($q: String!, $per_page: Int!) {
                findPerformers(filter: {q: $q, per_page: $per_page}) {
                    count
                    performers { id name image_path }
                }
            }"""
            res = stash_query(q, {"q": clean_search, "per_page": perf_limit})
            data = res.get("data", {}).get("findPerformers", {})
            total_count += data.get("count", 0)
            for p in data.get("performers", []):
                hint = {
                    "Name": p["name"],
                    "Id": f"performer-{p['id']}",
                    "ServerId": SERVER_ID,
                    "Type": "Person",
                    "MediaType": "",
                }
                if p.get("image_path"):
                    hint["PrimaryImageTag"] = "img"
                hints.append(hint)
    except Exception as e:
        logger.error(f"Search hints error: {e}")

    logger.debug(f"SearchHints '{clean_search}' -> {len(hints)} hints (total={total_count})")
    return JSONResponse({"SearchHints": hints, "TotalRecordCount": total_count})













# --- Stubs for endpoints real clients hit that we don't back with data ---
# Inventoried from live Infuse/Swiftfin/SenPlayer sessions. Each one was
# previously falling through to catch_all which returned an empty paginated
# result and a WARNING. Now each has a typed, shape-correct response so
# logs stay quiet and strict-schema clients don't see an unexpected shape.















_favicon_cache = None





async def endpoint_user_image(request):
    """Return a generated avatar for the user (shown pre-login in Swiftfin)."""
    img_data, content_type = generate_text_icon(SJS_USER or "?", width=200, height=200)
    return Response(content=img_data, media_type=content_type)








async def endpoint_websocket(websocket: WebSocket):
    """Jellyfin WebSocket endpoint for clients like Infuse-Direct that require it.

    Accepts the connection and runs a keepalive loop matching Jellyfin's protocol.
    Without this, newer Infuse versions hang for ~3s after login then retry indefinitely.
    """
    await websocket.accept()
    logger.debug(f"WebSocket connected: path={websocket.url.path} from {websocket.client}")
    try:
        # Send initial ForceKeepAlive so the client knows the interval (30s)
        await websocket.send_json({"MessageType": "ForceKeepAlive", "Data": 30})
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=60.0)
                msg_type = msg.get("MessageType", "")
                if msg_type == "KeepAlive":
                    await websocket.send_json({"MessageType": "KeepAlive"})
                # All other message types are acknowledged silently
            except asyncio.TimeoutError:
                # Send keepalive to prevent client disconnect
                await websocket.send_json({"MessageType": "KeepAlive"})
    except Exception as e:
        logger.debug(f"WebSocket disconnected: {e}")

# --- App Construction ---
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
    # Stubs for endpoints real iPad clients (Infuse/Swiftfin/SenPlayer)
    # poll that used to fall through to catch_all with a WARNING.
    Route("/Users", endpoint_users_list),
    Route("/Sessions", endpoint_sessions_list),
    Route("/System/Info/Storage", endpoint_system_info_storage),
    Route("/ScheduledTasks", endpoint_scheduled_tasks),
    Route("/web/ConfigurationPages", endpoint_web_configuration_pages),
    Route("/System/ActivityLog/Entries", endpoint_activity_log),
    Route("/System/Ext/ServerDomains", endpoint_server_domains),
    Route("/favicon.ico", endpoint_favicon),
    # User avatar alias — same handler as /UserImage for clients that
    # address it via the per-user URL.
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

# --- Global error handling contract -------------------------------------
# Per implementation plan §4.8. Endpoints will, over Phase 0.6 and the
# rebuild phases, replace ad-hoc try/except blocks with raises of the
# typed errors below. Until then, the global 500 handler catches anything
# that bubbles so we never leak a stack trace to clients or return HTML
# error pages to JSON-expecting Jellyfin clients.

class StashUnavailable(Exception):
    """Raised when Stash is unreachable (connection refused, timeout)."""


class StashError(Exception):
    """Raised when Stash returned GraphQL errors; detail carries the msg."""


class BadRequest(Exception):
    """Raised when a query param or body field is invalid/un-coercible."""
    def __init__(self, field: str, detail: str = ""):
        super().__init__(detail or field)
        self.field = field
        self.detail = detail or f"invalid value for '{field}'"


def _error_json(status: int, kind: str, **extra):
    payload = {"error": kind}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


async def _stash_unavailable_handler(request, exc):
    logger.error(f"stash_unavailable on {request.method} {request.url.path}: {exc}")
    return _error_json(503, "stash_unavailable")


async def _stash_error_handler(request, exc):
    logger.error(f"stash_error on {request.method} {request.url.path}: {exc}")
    return _error_json(502, "stash_error", detail=str(exc)[:200])


async def _bad_request_handler(request, exc):
    logger.info(f"bad_request on {request.method} {request.url.path}: {exc.field}: {exc.detail}")
    return _error_json(400, "bad_request", field=exc.field, detail=exc.detail)


async def _unhandled_exception_handler(request, exc):
    """Fallback. Logs the traceback at ERROR, returns JSON 500 so the
    client (Jellyfin Web, Infuse, Swiftfin) gets a parseable response
    instead of Starlette's HTML debug page."""
    import traceback
    tb = traceback.format_exc()
    logger.error(f"internal error on {request.method} {request.url.path}: {exc}\n{tb}")
    return _error_json(500, "internal")


# NOTE: do NOT register a catch-all Exception handler here. That catches
# ConnectionReset / asyncio.CancelledError during streaming responses
# before they reach RequestLoggingMiddleware's try/except, which is what
# logs "▶ Stream started" when the client disconnects. Swiftfin's player
# holds a single long-lived range request open and only disconnects at
# the end; catching the disconnect higher up silently stole that log
# event and left the Dashboard's active-stream list empty.
# If we need a generic 500 handler later, make it conditional on the
# response not having started, or move it into an ASGI middleware above
# RequestLoggingMiddleware so logging still fires.
_error_contract_handlers = {
    StashUnavailable: _stash_unavailable_handler,
    StashError: _stash_error_handler,
    BadRequest: _bad_request_handler,
}

middleware = [
    # CORS must sit above AuthenticationMiddleware. Browsers send OPTIONS
    # preflights without an Authorization header; if auth ran first it
    # would 401 the preflight and strip CORS headers, which the browser
    # reports as "TypeError: Failed to fetch" with no way to diagnose.
    # allow_private_network=True opts into Chrome's Private Network Access
    # spec, so clients whose DNS resolves us to a LAN IP (split-horizon)
    # aren't blocked.
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_private_network=True),
    Middleware(RequestLoggingMiddleware),
    Middleware(CaseInsensitivePathMiddleware),
    Middleware(AuthenticationMiddleware),
]

# debug=False in production so the Starlette debug 500 page doesn't leak
# tracebacks — our _unhandled_exception_handler returns JSON instead.
app = Starlette(debug=False, routes=routes, middleware=middleware,
                exception_handlers=_error_contract_handlers)

# --- Web UI Server ---
PROXY_RUNNING = False  # Track if proxy is running
PROXY_START_TIME = None  # Track when proxy started

# ui_index + ui_api_* endpoints (except ui_api_config which still
# mutates ~40 monolith config globals) live in proxy/ui/api.py.
from proxy.ui.api import (  # noqa: F401
    ui_index,
    ui_api_status,
    ui_api_logs,
    ui_api_streams,
    ui_api_stats,
    ui_api_stats_reset,
    ui_api_restart,
    ui_api_auth_config,
)



async def ui_api_config(request):
    """Get or set configuration."""
    # Declare globals at top of function (required before any reference)
    global TAG_GROUPS, FAVORITE_TAG, _favorite_tag_id_cache, LATEST_GROUPS, SERVER_NAME, STASH_TIMEOUT, STASH_RETRIES
    global STASH_GRAPHQL_PATH, STASH_VERIFY_TLS, ENABLE_FILTERS, ENABLE_IMAGE_RESIZE
    global ENABLE_TAG_FILTERS, ENABLE_ALL_TAGS
    global IMAGE_CACHE_MAX_SIZE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, REQUIRE_AUTH_FOR_CONFIG
    global LOG_LEVEL, _config_defined_keys, BANNED_IPS, BAN_THRESHOLD, BAN_WINDOW_MINUTES
    global BANNER_MODE, BANNER_POOL_SIZE, BANNER_TAGS

    if request.method == "GET":
        return JSONResponse({
            "config": {
                "STASH_URL": STASH_URL,
                "STASH_API_KEY": "*" * min(len(STASH_API_KEY), 20) if STASH_API_KEY else "",
                "STASH_GRAPHQL_PATH": STASH_GRAPHQL_PATH,
                "STASH_VERIFY_TLS": STASH_VERIFY_TLS,
                "PROXY_BIND": PROXY_BIND,
                "PROXY_PORT": PROXY_PORT,
                "UI_PORT": UI_PORT,
                "SJS_USER": SJS_USER,
                "SJS_PASSWORD": "*" * min(len(SJS_PASSWORD), 10) if SJS_PASSWORD else "",
                "SERVER_ID": SERVER_ID,
                "SERVER_NAME": SERVER_NAME,
                "TAG_GROUPS": TAG_GROUPS,
                "FAVORITE_TAG": FAVORITE_TAG,
                "LATEST_GROUPS": LATEST_GROUPS,
                "BANNER_MODE": BANNER_MODE,
                "BANNER_POOL_SIZE": BANNER_POOL_SIZE,
                "BANNER_TAGS": BANNER_TAGS,
                "STASH_TIMEOUT": STASH_TIMEOUT,
                "STASH_RETRIES": STASH_RETRIES,
                "ENABLE_FILTERS": ENABLE_FILTERS,
                "ENABLE_IMAGE_RESIZE": ENABLE_IMAGE_RESIZE,
                "ENABLE_TAG_FILTERS": ENABLE_TAG_FILTERS,
                "ENABLE_ALL_TAGS": ENABLE_ALL_TAGS,
                "REQUIRE_AUTH_FOR_CONFIG": REQUIRE_AUTH_FOR_CONFIG,
                "IMAGE_CACHE_MAX_SIZE": IMAGE_CACHE_MAX_SIZE,
                "DEFAULT_PAGE_SIZE": DEFAULT_PAGE_SIZE,
                "MAX_PAGE_SIZE": MAX_PAGE_SIZE,
                "LOG_LEVEL": LOG_LEVEL,
                "LOG_DIR": LOG_DIR,
                "LOG_FILE": LOG_FILE,
                "LOG_MAX_SIZE_MB": LOG_MAX_SIZE_MB,
                "LOG_BACKUP_COUNT": LOG_BACKUP_COUNT,
                "BAN_THRESHOLD": BAN_THRESHOLD,
                "BAN_WINDOW_MINUTES": BAN_WINDOW_MINUTES,
                "BANNED_IPS": ", ".join(sorted(BANNED_IPS)) if BANNED_IPS else ""
            },
            "env_fields": _env_overrides,
            "defined_fields": sorted(list(_config_defined_keys))
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
                "BAN_THRESHOLD", "BAN_WINDOW_MINUTES", "BANNED_IPS"
            ]

            # Sensitive keys - log changes but mask values
            sensitive_keys = ["STASH_API_KEY", "SJS_PASSWORD"]

            # Read existing config file preserving all lines
            original_lines = []
            existing_values = {}  # Currently active (uncommented) values
            all_keys_in_file = set()  # Track all keys in file (commented or not)
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
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
                "STASH_URL": STASH_URL,
                "STASH_API_KEY": STASH_API_KEY,
                "STASH_GRAPHQL_PATH": STASH_GRAPHQL_PATH,
                "STASH_VERIFY_TLS": "true" if STASH_VERIFY_TLS else "false",
                "PROXY_BIND": PROXY_BIND,
                "PROXY_PORT": str(PROXY_PORT),
                "UI_PORT": str(UI_PORT),
                "SJS_USER": SJS_USER,
                "SJS_PASSWORD": SJS_PASSWORD,
                "SERVER_ID": SERVER_ID,
                "SERVER_NAME": SERVER_NAME,
                "TAG_GROUPS": ", ".join(TAG_GROUPS) if TAG_GROUPS else "",
                "FAVORITE_TAG": FAVORITE_TAG,
                "LATEST_GROUPS": ", ".join(LATEST_GROUPS) if LATEST_GROUPS else "",
                "BANNER_MODE": BANNER_MODE,
                "BANNER_POOL_SIZE": str(BANNER_POOL_SIZE),
                "BANNER_TAGS": ", ".join(BANNER_TAGS) if BANNER_TAGS else "",
                "STASH_TIMEOUT": str(STASH_TIMEOUT),
                "STASH_RETRIES": str(STASH_RETRIES),
                "ENABLE_FILTERS": "true" if ENABLE_FILTERS else "false",
                "ENABLE_IMAGE_RESIZE": "true" if ENABLE_IMAGE_RESIZE else "false",
                "ENABLE_TAG_FILTERS": "true" if ENABLE_TAG_FILTERS else "false",
                "ENABLE_ALL_TAGS": "true" if ENABLE_ALL_TAGS else "false",
                "REQUIRE_AUTH_FOR_CONFIG": "true" if REQUIRE_AUTH_FOR_CONFIG else "false",
                "IMAGE_CACHE_MAX_SIZE": str(IMAGE_CACHE_MAX_SIZE),
                "DEFAULT_PAGE_SIZE": str(DEFAULT_PAGE_SIZE),
                "MAX_PAGE_SIZE": str(MAX_PAGE_SIZE),
                "LOG_LEVEL": LOG_LEVEL,
                "LOG_DIR": LOG_DIR,
                "LOG_FILE": LOG_FILE,
                "LOG_MAX_SIZE_MB": str(LOG_MAX_SIZE_MB),
                "LOG_BACKUP_COUNT": str(LOG_BACKUP_COUNT),
                "BAN_THRESHOLD": str(BAN_THRESHOLD),
                "BAN_WINDOW_MINUTES": str(BAN_WINDOW_MINUTES),
                "BANNED_IPS": ", ".join(sorted(BANNED_IPS)) if BANNED_IPS else "",
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
            with open(CONFIG_FILE, 'w') as f:
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
                    TAG_GROUPS = [t.strip() for t in new_val.split(",") if t.strip()]
                    applied_immediately.append(key)
                elif key == "FAVORITE_TAG":
                    FAVORITE_TAG = new_val.strip()
                    _favorite_tag_id_cache = None
                    applied_immediately.append(key)
                elif key == "LATEST_GROUPS":
                    LATEST_GROUPS = [t.strip() for t in new_val.split(",") if t.strip()]
                    applied_immediately.append(key)
                elif key == "BANNER_MODE":
                    m = new_val.strip().lower()
                    BANNER_MODE = m if m in ("recent", "tag") else "recent"
                    applied_immediately.append(key)
                elif key == "BANNER_POOL_SIZE":
                    try:
                        BANNER_POOL_SIZE = max(1, int(new_val))
                    except ValueError:
                        BANNER_POOL_SIZE = 200
                    applied_immediately.append(key)
                elif key == "BANNER_TAGS":
                    BANNER_TAGS = [t.strip() for t in new_val.split(",") if t.strip()]
                    applied_immediately.append(key)
                elif key == "SERVER_NAME":
                    SERVER_NAME = new_val
                    applied_immediately.append(key)
                elif key == "STASH_TIMEOUT":
                    STASH_TIMEOUT = int(new_val)
                    applied_immediately.append(key)
                elif key == "STASH_RETRIES":
                    STASH_RETRIES = int(new_val)
                    applied_immediately.append(key)
                elif key == "STASH_GRAPHQL_PATH":
                    STASH_GRAPHQL_PATH = normalize_path(new_val)
                    applied_immediately.append(key)
                elif key == "STASH_VERIFY_TLS":
                    STASH_VERIFY_TLS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_FILTERS":
                    ENABLE_FILTERS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_IMAGE_RESIZE":
                    ENABLE_IMAGE_RESIZE = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_TAG_FILTERS":
                    ENABLE_TAG_FILTERS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "ENABLE_ALL_TAGS":
                    ENABLE_ALL_TAGS = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "IMAGE_CACHE_MAX_SIZE":
                    IMAGE_CACHE_MAX_SIZE = int(new_val)
                    applied_immediately.append(key)
                elif key == "DEFAULT_PAGE_SIZE":
                    DEFAULT_PAGE_SIZE = int(new_val)
                    applied_immediately.append(key)
                elif key == "MAX_PAGE_SIZE":
                    MAX_PAGE_SIZE = int(new_val)
                    applied_immediately.append(key)
                elif key == "REQUIRE_AUTH_FOR_CONFIG":
                    REQUIRE_AUTH_FOR_CONFIG = new_val.lower() in ('true', 'yes', '1', 'on')
                    applied_immediately.append(key)
                elif key == "LOG_LEVEL":
                    LOG_LEVEL = new_val.upper()
                    # Update logger level
                    level = getattr(logging, LOG_LEVEL, logging.INFO)
                    logger.setLevel(level)
                    for handler in logger.handlers:
                        handler.setLevel(level)
                    applied_immediately.append(key)
                elif key == "BAN_THRESHOLD":
                    BAN_THRESHOLD = int(new_val)
                    applied_immediately.append(key)
                elif key == "BAN_WINDOW_MINUTES":
                    BAN_WINDOW_MINUTES = int(new_val)
                    applied_immediately.append(key)
                elif key == "BANNED_IPS":
                    BANNED_IPS = set(ip.strip() for ip in new_val.split(",") if ip.strip())
                    applied_immediately.append(key)
                elif key in ["PROXY_BIND", "PROXY_PORT", "UI_PORT", "LOG_DIR", "LOG_FILE",
                             "STASH_URL", "STASH_API_KEY", "SJS_USER", "SJS_PASSWORD", "SERVER_ID"]:
                    needs_restart.append(key)

            # Apply default values for commented-out keys
            for key in commented_keys:
                default_val = defaults.get(key, "")
                if key == "TAG_GROUPS":
                    TAG_GROUPS = []
                    applied_immediately.append(key)
                elif key == "FAVORITE_TAG":
                    FAVORITE_TAG = ""
                    _favorite_tag_id_cache = None
                    applied_immediately.append(key)
                elif key == "LATEST_GROUPS":
                    LATEST_GROUPS = []
                    applied_immediately.append(key)
                elif key == "BANNER_MODE":
                    BANNER_MODE = "recent"
                    applied_immediately.append(key)
                elif key == "BANNER_POOL_SIZE":
                    BANNER_POOL_SIZE = 200
                    applied_immediately.append(key)
                elif key == "BANNER_TAGS":
                    BANNER_TAGS = []
                    applied_immediately.append(key)
                elif key == "SERVER_NAME":
                    SERVER_NAME = "Stash Media Server"
                    applied_immediately.append(key)
                elif key == "STASH_TIMEOUT":
                    STASH_TIMEOUT = 30
                    applied_immediately.append(key)
                elif key == "STASH_RETRIES":
                    STASH_RETRIES = 3
                    applied_immediately.append(key)
                elif key == "STASH_GRAPHQL_PATH":
                    STASH_GRAPHQL_PATH = "/graphql"
                    applied_immediately.append(key)
                elif key == "STASH_VERIFY_TLS":
                    STASH_VERIFY_TLS = False
                    applied_immediately.append(key)
                elif key == "ENABLE_FILTERS":
                    ENABLE_FILTERS = True
                    applied_immediately.append(key)
                elif key == "ENABLE_IMAGE_RESIZE":
                    ENABLE_IMAGE_RESIZE = True
                    applied_immediately.append(key)
                elif key == "ENABLE_TAG_FILTERS":
                    ENABLE_TAG_FILTERS = False
                    applied_immediately.append(key)
                elif key == "ENABLE_ALL_TAGS":
                    ENABLE_ALL_TAGS = False
                    applied_immediately.append(key)
                elif key == "IMAGE_CACHE_MAX_SIZE":
                    IMAGE_CACHE_MAX_SIZE = 100
                    applied_immediately.append(key)
                elif key == "DEFAULT_PAGE_SIZE":
                    DEFAULT_PAGE_SIZE = 50
                    applied_immediately.append(key)
                elif key == "MAX_PAGE_SIZE":
                    MAX_PAGE_SIZE = 200
                    applied_immediately.append(key)
                elif key == "REQUIRE_AUTH_FOR_CONFIG":
                    REQUIRE_AUTH_FOR_CONFIG = False
                    applied_immediately.append(key)
                elif key == "LOG_LEVEL":
                    LOG_LEVEL = "INFO"
                    logger.setLevel(logging.INFO)
                    for handler in logger.handlers:
                        handler.setLevel(logging.INFO)
                    applied_immediately.append(key)
                elif key == "BAN_THRESHOLD":
                    BAN_THRESHOLD = 10
                    applied_immediately.append(key)
                elif key == "BAN_WINDOW_MINUTES":
                    BAN_WINDOW_MINUTES = 15
                    applied_immediately.append(key)
                elif key == "BANNED_IPS":
                    BANNED_IPS = set()
                    applied_immediately.append(key)

            # Update _config_defined_keys to reflect new state
            for key in updates:
                _config_defined_keys.add(key)
            for key in commented_keys:
                _config_defined_keys.discard(key)

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





# Global reference for restart functionality
_shutdown_event = None
_restart_requested = False



ui_routes = [
    Route("/", ui_index),
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

# --- Hypercorn Disconnect Error Filter ---
class SuppressDisconnectFilter(logging.Filter):
    """Filter to suppress expected socket disconnect errors from Hypercorn."""

    def filter(self, record):
        # Suppress "socket.send() raised exception" messages
        msg = record.getMessage()
        if "socket.send() raised exception" in msg:
            return False
        if "socket.recv() raised exception" in msg:
            return False

        # Also suppress common disconnect exception types
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type in (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                return False

        return True

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
