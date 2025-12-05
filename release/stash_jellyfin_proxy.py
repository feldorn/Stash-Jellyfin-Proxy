#!/usr/bin/env python3
"""
Stash-Jellyfin Proxy v5.00
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
import argparse
import time
import re
from typing import Optional, List, Dict, Any, Tuple
from logging.handlers import SysLogHandler, RotatingFileHandler

# Third-party dependencies
try:
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response, RedirectResponse
    from starlette.routing import Route
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

# Fallback placeholder PNG (400x600 dark blue) - base64 decoded at runtime if needed
# This is used when Pillow is not available or font generation fails
PLACEHOLDER_PNG = None
def _init_placeholder_png():
    """Generate a 400x600 dark blue PNG placeholder image."""
    global PLACEHOLDER_PNG
    if PILLOW_AVAILABLE:
        try:
            img = Image.new('RGB', (400, 600), (26, 26, 46))
            output = io.BytesIO()
            img.save(output, format='PNG')
            PLACEHOLDER_PNG = output.getvalue()
        except Exception:
            pass
    if PLACEHOLDER_PNG is None:
        # Minimal 1x1 dark PNG as last resort
        PLACEHOLDER_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'

_init_placeholder_png()

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

# Latest groups - controls which libraries show on Infuse home page
# "Scenes" = all scenes, other entries must match TAG_GROUPS entries
LATEST_GROUPS = ["Scenes"]  # e.g., ["Scenes", "VR", "Favorites"]

# Server identity
SERVER_NAME = "Stash Media Server"
SERVER_ID = ""  # Required - must be set in config file

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

# IP Ban settings
BANNED_IPS = set()  # Set of banned IP addresses
BAN_THRESHOLD = 10  # Failed attempts before ban
BAN_WINDOW_MINUTES = 15  # Rolling window for counting failures

# Load Config - parses config file with KEY = "value" or KEY="value" format
def load_config(filepath):
    """Load configuration from a shell-style config file.
    Returns (config_dict, defined_keys_set) where defined_keys_set tracks
    which keys were explicitly defined in the file."""
    config = {}
    defined_keys = set()
    if os.path.isfile(filepath):
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    # Parse KEY=value or KEY="value" format
                    if '=' in line:
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        config[key] = value
                        defined_keys.add(key)
        except Exception as e:
            print(f"Error loading config file {filepath}: {e}", file=sys.stderr)
    return config, defined_keys

def parse_bool(value, default=True):
    """Parse a boolean value from config string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', 'yes', '1', 'on')
    return default

def normalize_path(path, default="/graphql"):
    """Normalize a path: ensure leading /, remove trailing /."""
    if not path or not path.strip():
        return default
    p = path.strip()
    if not p.startswith('/'):
        p = '/' + p
    if len(p) > 1 and p.endswith('/'):
        p = p.rstrip('/')
    return p

def generate_server_id():
    """Generate a random 32-character server ID (like UUID without dashes)."""
    import uuid
    return uuid.uuid4().hex

def save_server_id_to_config(config_file, server_id):
    """Save SERVER_ID to config file, updating existing entry or adding new one."""
    if not os.path.isfile(config_file):
        # Create minimal config file with just SERVER_ID
        with open(config_file, 'w') as f:
            f.write(f'# Auto-generated config\nSERVER_ID = {server_id}\n')
        return True

    # Read existing config
    with open(config_file, 'r') as f:
        lines = f.readlines()

    # Try to find and update SERVER_ID line
    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        # Match both commented and uncommented SERVER_ID lines
        if stripped.startswith('#') and 'SERVER_ID' in stripped and '=' in stripped:
            # Commented SERVER_ID - uncomment and set value
            new_lines.append(f'SERVER_ID = {server_id}\n')
            updated = True
        elif stripped.startswith('SERVER_ID') and '=' in stripped:
            # Existing SERVER_ID - update value
            new_lines.append(f'SERVER_ID = {server_id}\n')
            updated = True
        else:
            new_lines.append(line)

    # If no SERVER_ID line found, append it
    if not updated:
        new_lines.append(f'\n# Server identification (auto-generated)\nSERVER_ID = {server_id}\n')

    # Write back
    with open(config_file, 'w') as f:
        f.writelines(new_lines)
    return True

_config, _config_defined_keys = load_config(CONFIG_FILE)
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
    # Parse LATEST_GROUPS as comma-separated list
    latest_groups_str = _config.get("LATEST_GROUPS", "")
    if latest_groups_str:
        LATEST_GROUPS = [t.strip() for t in latest_groups_str.split(",") if t.strip()]

    # Server identity
    SERVER_NAME = _config.get("SERVER_NAME", SERVER_NAME)
    SERVER_ID = _config.get("SERVER_ID", SERVER_ID)

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
        IMAGE_CACHE_MAX_SIZE = int(_config.get("IMAGE_CACHE_MAX_SIZE", IMAGE_CACHE_MAX_SIZE))

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
if LATEST_GROUPS:
    print(f"  Latest groups: {', '.join(LATEST_GROUPS)}")

# Auto-generate SERVER_ID if not set
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

# Session management for cookie-based auth
STASH_SESSION = None  # Will hold requests.Session with auth cookies

# Image cache for resized studio/performer images (prevents repeated processing)
IMAGE_CACHE = {}  # Key: (item_id, target_size), Value: (bytes, content_type)
IMAGE_CACHE_MAX_SIZE = 100  # Max items to cache

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
WEB_UI_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{SERVER_NAME}}</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg-primary: #0f0f1a;
            --bg-secondary: #1a1a2e;
            --bg-card: #252540;
            --text-primary: #e0e0e0;
            --text-secondary: #a0a0a0;
            --accent: #4a90d9;
            --accent-hover: #5da0e9;
            --success: #4caf50;
            --warning: #ff9800;
            --error: #f44336;
            --border: #3a3a5a;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
        }
        .layout {
            display: flex;
            min-height: 100vh;
        }
        .sidebar {
            width: 220px;
            background: var(--bg-secondary);
            padding: 20px 0;
            border-right: 1px solid var(--border);
        }
        .logo {
            padding: 0 20px 20px;
            border-bottom: 1px solid var(--border);
            margin-bottom: 20px;
        }
        .logo h1 {
            font-size: 16px;
            color: var(--accent);
        }
        .logo span {
            font-size: 12px;
            color: var(--text-secondary);
        }
        .nav-item {
            display: flex;
            align-items: center;
            padding: 12px 20px;
            color: var(--text-secondary);
            text-decoration: none;
            cursor: pointer;
            transition: all 0.2s;
        }
        .nav-item:hover, .nav-item.active {
            background: var(--bg-card);
            color: var(--text-primary);
        }
        .nav-item.active {
            border-left: 3px solid var(--accent);
        }
        .nav-item svg {
            width: 20px;
            height: 20px;
            margin-right: 12px;
        }
        .main {
            flex: 1;
            padding: 30px;
            overflow-y: auto;
        }
        .page-title {
            font-size: 24px;
            margin-bottom: 24px;
        }
        .card {
            background: var(--bg-card);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .card-title {
            font-size: 14px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 16px;
        }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .status-card {
            background: var(--bg-card);
            border-radius: 8px;
            padding: 20px;
        }
        .status-label {
            font-size: 12px;
            color: var(--text-secondary);
            text-transform: uppercase;
            margin-bottom: 8px;
        }
        .status-value {
            font-size: 24px;
            font-weight: 600;
        }
        .status-value.running { color: var(--success); }
        .status-value.stopped { color: var(--error); }
        .status-value.connected { color: var(--success); }
        .status-value.disconnected { color: var(--error); }
        .form-group {
            margin-bottom: 16px;
        }
        .form-label {
            display: block;
            font-size: 14px;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }
        .form-input {
            width: 100%;
            padding: 10px 12px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 14px;
        }
        .form-input:focus {
            outline: none;
            border-color: var(--accent);
        }
        .form-hint, .status-hint {
            font-size: 12px;
            color: var(--text-secondary);
            margin-top: 4px;
        }
        .env-locked {
            border-color: #b35900 !important;
            background: rgba(179, 89, 0, 0.15) !important;
            color: #ff9933 !important;
            cursor: not-allowed;
        }
        .env-locked:disabled {
            opacity: 0.8;
        }
        .env-locked-label {
            color: #ff9933 !important;
            cursor: not-allowed;
            opacity: 0.8;
        }
        .env-locked-label::after {
            content: ' (env)';
            font-size: 11px;
            color: #b35900;
        }
        .env-notice {
            background: rgba(179, 89, 0, 0.15);
            border: 1px solid #b35900;
            border-radius: 6px;
            padding: 12px 16px;
            margin-top: 16px;
            color: #ff9933;
            font-size: 13px;
            display: none;
        }
        .env-notice strong {
            color: #ffb366;
        }
        .form-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
        }
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-primary {
            background: var(--accent);
            color: white;
        }
        .btn-primary:hover {
            background: var(--accent-hover);
        }
        .btn-secondary {
            background: var(--bg-secondary);
            color: var(--text-primary);
            border: 1px solid var(--border);
        }
        .log-viewer {
            background: var(--bg-secondary);
            border-radius: 6px;
            padding: 16px;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 12px;
            line-height: 1.6;
            max-height: 400px;
            overflow-y: auto;
        }
        .log-entry {
            padding: 2px 0;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .log-DEBUG { color: #888; }
        .log-INFO { color: var(--text-primary); }
        .log-WARNING { color: var(--warning); }
        .log-ERROR { color: var(--error); }
        .log-controls {
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            align-items: center;
        }
        .log-filter {
            padding: 8px 12px;
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 14px;
        }
        .log-count {
            margin-left: auto;
            color: var(--text-secondary);
            font-size: 14px;
        }
        .streams-list {
            background: var(--bg-secondary);
            border-radius: 6px;
            padding: 16px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(80px, 1fr));
            gap: 12px;
        }
        .stat-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 12px 8px;
            background: var(--bg-secondary);
            border-radius: 6px;
            text-align: center;
        }
        .stat-value {
            font-size: 1.5rem;
            font-weight: 600;
            color: var(--accent);
        }
        .stat-label {
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 4px;
        }
        .top-played-list {
            background: var(--bg-secondary);
            border-radius: 6px;
            padding: 12px;
        }
        .top-played-item {
            display: flex;
            align-items: center;
            padding: 10px 12px;
            background: var(--bg-card);
            border-radius: 6px;
            margin-bottom: 8px;
        }
        .top-played-item:last-child {
            margin-bottom: 0;
        }
        .top-played-rank {
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--accent);
            width: 30px;
            text-align: center;
        }
        .top-played-info {
            flex: 1;
            margin-left: 12px;
            overflow: hidden;
        }
        .top-played-title {
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .top-played-performer {
            font-size: 0.85rem;
            color: var(--text-muted);
        }
        .top-played-count {
            font-weight: 600;
            color: var(--accent);
            margin-left: 12px;
        }
        .stream-item {
            display: flex;
            flex-direction: column;
            padding: 12px;
            background: var(--bg-card);
            border-radius: 6px;
            margin-bottom: 8px;
        }
        .stream-header {
            display: flex;
            align-items: center;
            margin-bottom: 6px;
        }
        .stream-title {
            flex: 1;
            font-weight: 500;
        }
        .stream-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            font-size: 13px;
            color: var(--text-secondary);
        }
        .stream-meta-item {
            display: flex;
            align-items: center;
            gap: 4px;
        }
        .stream-meta-item svg {
            width: 14px;
            height: 14px;
        }
        .stream-time {
            color: var(--text-secondary);
            font-size: 12px;
        }
        .empty-state {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            padding: 12px 20px;
            border-radius: 6px;
            color: white;
            font-size: 14px;
            z-index: 1000;
            animation: slideIn 0.3s ease;
        }
        .toast.success { background: var(--success); }
        .toast.error { background: var(--error); }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        .hidden { display: none; }
        select.form-input {
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div class="layout">
        <nav class="sidebar">
            <div class="logo">
                <h1>Stash-Jellyfin Proxy</h1>
                <span id="version">v5.00</span>
            </div>
            <a class="nav-item active" data-page="dashboard">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
                Dashboard
            </a>
            <a class="nav-item" data-page="config">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path></svg>
                Configuration
            </a>
            <a class="nav-item" data-page="logs">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                Logs
            </a>
        </nav>
        <main class="main">
            <!-- Dashboard Page -->
            <div id="page-dashboard" class="page">
                <h2 class="page-title">Dashboard</h2>
                <div class="status-grid">
                    <div class="status-card">
                        <div class="status-label">Proxy Status</div>
                        <div id="proxy-status" class="status-value">Checking...</div>
                        <div id="proxy-uptime" class="status-hint"></div>
                    </div>
                    <div class="status-card">
                        <div class="status-label">Stash Connection</div>
                        <div id="stash-status" class="status-value">Checking...</div>
                    </div>
                    <div class="status-card">
                        <div class="status-label">Stash Version</div>
                        <div id="stash-version" class="status-value">-</div>
                    </div>
                    <div class="status-card">
                        <div class="status-label">Active Streams</div>
                        <div id="stream-count" class="status-value">0</div>
                    </div>
                </div>
                <div class="card">
                    <h3 class="card-title">Active Streams</h3>
                    <div id="streams-list" class="streams-list">
                        <div class="empty-state">No active streams</div>
                    </div>
                </div>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;">
                    <div class="card">
                        <h3 class="card-title">Stash Library</h3>
                        <div class="stats-grid" id="stash-library-stats">
                            <div class="stat-item"><span class="stat-value" id="stat-scenes">-</span><span class="stat-label">Scenes</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-performers">-</span><span class="stat-label">Performers</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-studios">-</span><span class="stat-label">Studios</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-tags">-</span><span class="stat-label">Tags</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-groups">-</span><span class="stat-label">Groups</span></div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title" style="display: flex; justify-content: space-between; align-items: center;">
                            Proxy Usage
                            <button type="button" id="reset-stats-btn" class="btn" style="padding: 0.25rem 0.5rem; font-size: 0.75rem;">Reset</button>
                        </h3>
                        <div class="stats-grid" id="proxy-usage-stats">
                            <div class="stat-item"><span class="stat-value" id="stat-streams-today">-</span><span class="stat-label">Streams Today</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-total-streams">-</span><span class="stat-label">Total Streams</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-unique-ips">-</span><span class="stat-label">Clients Today</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-auth-success">-</span><span class="stat-label">Auth Success</span></div>
                            <div class="stat-item"><span class="stat-value" id="stat-auth-failed">-</span><span class="stat-label">Auth Failed</span></div>
                        </div>
                    </div>
                </div>
                <div class="card">
                    <h3 class="card-title">Top Played</h3>
                    <div id="top-played-list" class="top-played-list">
                        <div class="empty-state">No play data yet</div>
                    </div>
                </div>
                <div class="card">
                    <h3 class="card-title">Recent Logs</h3>
                    <div id="dashboard-logs" class="log-viewer"></div>
                </div>
            </div>
            <!-- Configuration Page -->
            <div id="page-config" class="page hidden">
                <h2 class="page-title">Configuration</h2>
                <form id="config-form" autocomplete="off">
                    <div class="card">
                        <h3 class="card-title">Stash Connection</h3>
                        <div class="form-hint" style="margin-bottom: 1rem; padding: 0.5rem; background: rgba(255,193,7,0.1); border-radius: 4px;">⚠️ All settings in this section require a server restart to take effect.</div>
                        <div class="form-group">
                            <label class="form-label">Stash URL</label>
                            <input type="text" class="form-input" name="STASH_URL" placeholder="https://stash:9999">
                            <div class="form-hint">Full URL including port (e.g., http://localhost:9999 or https://stash.example.com)</div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">API Key</label>
                            <input type="password" class="form-input" name="STASH_API_KEY" placeholder="Enter API key" autocomplete="off" data-1p-ignore data-lpignore="true">
                            <div class="form-hint">Found in Stash: Settings → Security → API Key</div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">GraphQL Path</label>
                            <input type="text" class="form-input" name="STASH_GRAPHQL_PATH" placeholder="/graphql">
                            <div class="form-hint">Usually /graphql but can be overridden for advanced proxy configurations</div>
                        </div>
                        <div class="form-group">
                            <label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">
                                <input type="checkbox" name="STASH_VERIFY_TLS" checked style="width: auto;">
                                Verify TLS Certificate
                            </label>
                            <div class="form-hint">Disable for self-signed certificates (not recommended for production)</div>
                        </div>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Timeout (seconds)</label>
                                <input type="number" class="form-input" name="STASH_TIMEOUT" placeholder="30">
                                <div class="form-hint">API request timeout</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Retries</label>
                                <input type="number" class="form-input" name="STASH_RETRIES" placeholder="3">
                                <div class="form-hint">Retry count on failure</div>
                            </div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Proxy Settings</h3>
                        <div class="form-hint" style="margin-bottom: 1rem; padding: 0.5rem; background: rgba(255,193,7,0.1); border-radius: 4px;">⚠️ All settings in this section require a server restart to take effect.</div>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Bind Address</label>
                                <input type="text" class="form-input" name="PROXY_BIND" placeholder="0.0.0.0">
                                <div class="form-hint">Use 0.0.0.0 for all interfaces, or 127.0.0.1 for local only</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Proxy Port</label>
                                <input type="number" class="form-input" name="PROXY_PORT" placeholder="8096">
                                <div class="form-hint">Port for Jellyfin API (default 8096)</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">UI Port</label>
                                <input type="number" class="form-input" name="UI_PORT" placeholder="8097">
                                <div class="form-hint">Port for this Web UI (0 to disable)</div>
                            </div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Authentication</h3>
                        <div class="form-hint" style="margin-bottom: 1rem; padding: 0.5rem; background: rgba(255,193,7,0.1); border-radius: 4px;">⚠️ Username and Password changes require a server restart.</div>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Username</label>
                                <input type="text" class="form-input" name="SJS_USER" placeholder="e.g. admin" autocomplete="off" data-1p-ignore data-lpignore="true">
                                <div class="form-hint">Username for connecting from Infuse</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Password</label>
                                <input type="password" class="form-input" name="SJS_PASSWORD" placeholder="Enter password" autocomplete="new-password" data-1p-ignore data-lpignore="true">
                                <div class="form-hint">Password for connecting from Infuse</div>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">
                                <input type="checkbox" name="REQUIRE_AUTH_FOR_CONFIG" style="width: auto;">
                                Require Password for Configuration
                            </label>
                            <div class="form-hint">Prompt for password before accessing this config page</div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Server Identity</h3>
                        <div class="form-group">
                            <label class="form-label">Server ID</label>
                            <input type="text" class="form-input" name="SERVER_ID">
                            <div class="form-hint" style="color: var(--warning);">Warning: Changing this value will break existing client pairings. Changes require a server restart.</div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Server Name</label>
                            <input type="text" class="form-input" name="SERVER_NAME" placeholder="Stash Media Server">
                            <div class="form-hint">Display name shown in Infuse and other clients</div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Library Organization</h3>
                        <div class="form-group">
                            <label class="form-label">Tag Groups</label>
                            <input type="text" class="form-input" name="TAG_GROUPS" placeholder="e.g. Favorites, VR, 4K">
                            <div class="form-hint">Comma-separated Stash tag names to create as library folders in Infuse</div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Latest Groups</label>
                            <input type="text" class="form-input" name="LATEST_GROUPS" placeholder="Scenes">
                            <div class="form-hint">Libraries to show on Infuse home screen (use "Scenes" for all scenes)</div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Feature Toggles</h3>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">
                                    <input type="checkbox" name="ENABLE_FILTERS" checked style="width: auto;">
                                    Enable Scene Filters
                                </label>
                                <div class="form-hint">Show FILTERS folder in library (uses saved scene filters from Stash)</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">
                                    <input type="checkbox" name="ENABLE_IMAGE_RESIZE" checked style="width: auto;">
                                    Enable Image Resize
                                </label>
                                <div class="form-hint">Resize studio logos to fit Infuse tiles better</div>
                            </div>
                        </div>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">
                                    <input type="checkbox" name="ENABLE_TAG_FILTERS" style="width: auto;">
                                    Enable Tag Filters
                                </label>
                                <div class="form-hint">Show Tags folder for browsing tags and their scenes</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label" style="display: flex; align-items: center; gap: 0.5rem;">
                                    <input type="checkbox" name="ENABLE_ALL_TAGS" style="width: auto;">
                                    Enable All Tags
                                </label>
                                <div class="form-hint">Show "All Tags" subfolder (may be large in some libraries)</div>
                            </div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Pagination</h3>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Default Page Size</label>
                                <input type="number" class="form-input" name="DEFAULT_PAGE_SIZE" placeholder="50">
                                <div class="form-hint">Items per page when Infuse doesn't specify a limit</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Max Page Size</label>
                                <input type="number" class="form-input" name="MAX_PAGE_SIZE" placeholder="200">
                                <div class="form-hint">Maximum items allowed in a single API request</div>
                            </div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Logging</h3>
                        <div class="form-hint" style="margin-bottom: 1rem; padding: 0.5rem; background: rgba(255,193,7,0.1); border-radius: 4px;">⚠️ Log Directory and Log File changes require a server restart.</div>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Log Level</label>
                                <select class="form-input" name="LOG_LEVEL">
                                    <option value="DEBUG">DEBUG</option>
                                    <option value="INFO">INFO</option>
                                    <option value="WARNING">WARNING</option>
                                    <option value="ERROR">ERROR</option>
                                </select>
                                <div class="form-hint">DEBUG shows all details, INFO shows key events only</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Log Directory</label>
                                <input type="text" class="form-input" name="LOG_DIR" placeholder="/config">
                                <div class="form-hint">Directory for log files</div>
                            </div>
                        </div>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Log File</label>
                                <input type="text" class="form-input" name="LOG_FILE" placeholder="stash_jellyfin_proxy.log">
                                <div class="form-hint">Log filename</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Max Size (MB)</label>
                                <input type="number" class="form-input" name="LOG_MAX_SIZE_MB" placeholder="10">
                                <div class="form-hint">Max file size before rotation</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Backup Count</label>
                                <input type="number" class="form-input" name="LOG_BACKUP_COUNT" placeholder="3">
                                <div class="form-hint">Old log files to keep</div>
                            </div>
                        </div>
                    </div>
                    <div class="card">
                        <h3 class="card-title">Security - IP Banning</h3>
                        <div class="form-row">
                            <div class="form-group">
                                <label class="form-label">Ban Threshold</label>
                                <input type="number" class="form-input" name="BAN_THRESHOLD" placeholder="10">
                                <div class="form-hint">Failed auth attempts before auto-ban</div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Ban Window (minutes)</label>
                                <input type="number" class="form-input" name="BAN_WINDOW_MINUTES" placeholder="15">
                                <div class="form-hint">Rolling window for counting failures</div>
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label">Banned IPs</label>
                            <textarea class="form-input" name="BANNED_IPS" rows="3" placeholder="e.g. 192.168.1.100, 10.0.0.50" style="resize: vertical;"></textarea>
                            <div class="form-hint">Comma-separated list of banned IP addresses. Edit to add/remove bans manually.</div>
                        </div>
                    </div>
                    <div id="env-notice" class="env-notice">
                        <strong>Environment Override:</strong> The following fields are set via environment variables and cannot be changed here: <span></span>
                    </div>
                    <div style="display: flex; gap: 1rem; align-items: center; margin-top: 16px;">
                        <button type="submit" class="btn btn-primary">Save Configuration</button>
                        <button type="button" id="restart-btn" class="btn" style="background: #dc3545;">Restart Server</button>
                    </div>
                    <p id="restart-note" class="hidden" style="color: #ffc107; margin-top: 0.5rem; font-size: 0.9rem;">Note: Changes to bind address or ports require a restart to take effect.</p>
                </form>
            </div>
            <!-- Logs Page -->
            <div id="page-logs" class="page hidden">
                <h2 class="page-title">Logs</h2>
                <div class="card">
                    <div class="log-controls">
                        <select id="log-level-filter" class="log-filter">
                            <option value="">All Levels</option>
                            <option value="DEBUG">DEBUG</option>
                            <option value="INFO">INFO</option>
                            <option value="WARNING">WARNING</option>
                            <option value="ERROR">ERROR</option>
                        </select>
                        <input type="text" id="log-search" class="form-input" placeholder="Search logs..." style="width: 300px;">
                        <span id="log-count" class="log-count">0 entries</span>
                        <button id="download-logs" class="btn btn-secondary">Download</button>
                    </div>
                    <div id="full-logs" class="log-viewer" style="max-height: 600px;"></div>
                </div>
            </div>
        </main>
    </div>
    <script>
        const state = {
            config: {},
            logs: [],
            streams: [],
            currentPage: 'dashboard',
            configAuthenticated: false
        };

        // Helper to format duration in human-readable format
        function formatDuration(seconds) {
            if (!seconds || seconds < 0) return '';
            const d = Math.floor(seconds / 86400);
            const h = Math.floor((seconds % 86400) / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            if (d > 0) return `${d}d ${h}h ${m}m`;
            if (h > 0) return `${h}h ${m}m`;
            if (m > 0) return `${m}m ${s}s`;
            return `${s}s`;
        }

        // Navigation
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                const page = item.dataset.page;
                showPage(page);
            });
        });

        async function showPage(page) {
            // Check if config page requires authentication
            if (page === 'config' && state.config.REQUIRE_AUTH_FOR_CONFIG && !state.configAuthenticated) {
                const password = prompt('Enter password to access configuration:');
                if (!password) return;

                try {
                    const res = await fetch('/api/auth-config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password })
                    });
                    const data = await res.json();
                    if (!data.success) {
                        alert('Incorrect password');
                        return;
                    }
                    state.configAuthenticated = true;
                } catch (e) {
                    alert('Authentication failed');
                    return;
                }
            }

            state.currentPage = page;
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            document.querySelector(`[data-page="${page}"]`).classList.add('active');
            document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
            document.getElementById(`page-${page}`).classList.remove('hidden');

            // Refresh data when switching pages
            if (page === 'config') {
                fetchConfig();
            } else if (page === 'logs') {
                fetchLogs();
            }
        }

        // API calls
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                document.getElementById('proxy-status').textContent = data.running ? 'Running' : 'Stopped';
                document.getElementById('proxy-status').className = 'status-value ' + (data.running ? 'running' : 'stopped');
                document.getElementById('stash-status').textContent = data.stashConnected ? 'Connected' : 'Disconnected';
                document.getElementById('stash-status').className = 'status-value ' + (data.stashConnected ? 'connected' : 'disconnected');
                document.getElementById('stash-version').textContent = data.stashVersion || '-';
                document.getElementById('version').textContent = data.version || 'v5.00';
                document.getElementById('proxy-uptime').textContent = data.uptime ? `Uptime: ${formatDuration(data.uptime)}` : '';
            } catch (e) {
                console.error('Failed to fetch status:', e);
            }
        }

        async function fetchStreams() {
            try {
                const res = await fetch('/api/streams');
                const data = await res.json();
                state.streams = data.streams || [];
                document.getElementById('stream-count').textContent = state.streams.length;
                const list = document.getElementById('streams-list');
                if (state.streams.length === 0) {
                    list.innerHTML = '<div class="empty-state">No active streams</div>';
                } else {
                    list.innerHTML = state.streams.map(s => {
                        const startedAt = s.started ? new Date(s.started * 1000).toLocaleTimeString() : '';
                        const duration = s.started ? formatDuration(Date.now()/1000 - s.started) : '';
                        return `
                        <div class="stream-item">
                            <div class="stream-header">
                                <span class="stream-title">${s.performer ? s.performer + ': ' : ''}${s.title || s.id}</span>
                                <span class="stream-time">${duration}</span>
                            </div>
                            <div class="stream-meta">
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"></path></svg>
                                    ${s.user || 'unknown'}
                                </span>
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                                    Started ${startedAt}
                                </span>
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"></path></svg>
                                    ${s.clientIp || 'unknown'}
                                </span>
                                <span class="stream-meta-item">
                                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"></path></svg>
                                    ${s.clientType || 'unknown'}
                                </span>
                            </div>
                        </div>
                    `;}).join('');
                }
            } catch (e) {
                console.error('Failed to fetch streams:', e);
            }
        }

        async function fetchLogs() {
            try {
                const res = await fetch('/api/logs?limit=100');
                const data = await res.json();
                state.logs = data.entries || [];
                renderLogs();
            } catch (e) {
                console.error('Failed to fetch logs:', e);
            }
        }

        function renderLogs() {
            const levelFilter = document.getElementById('log-level-filter').value;
            const searchFilter = document.getElementById('log-search').value.toLowerCase();
            let filtered = state.logs;
            if (levelFilter) {
                filtered = filtered.filter(l => l.level === levelFilter);
            }
            if (searchFilter) {
                filtered = filtered.filter(l => l.message.toLowerCase().includes(searchFilter));
            }
            document.getElementById('log-count').textContent = `${filtered.length} entries`;
            const html = filtered.map(l => `<div class="log-entry log-${l.level}">${l.timestamp} [${l.level}] ${l.message}</div>`).join('');
            document.getElementById('full-logs').innerHTML = html || '<div class="empty-state">No logs</div>';
            // Dashboard shows last 10 log entries (not sliced by character count)
            const recentLogs = filtered.slice(-10);
            const dashboardHtml = recentLogs.map(l => `<div class="log-entry log-${l.level}">${l.timestamp} [${l.level}] ${l.message}</div>`).join('');
            document.getElementById('dashboard-logs').innerHTML = dashboardHtml || '<div class="empty-state">No logs</div>';
        }

        async function fetchStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();

                // Update Stash library stats
                if (data.stash) {
                    document.getElementById('stat-scenes').textContent = data.stash.scenes.toLocaleString();
                    document.getElementById('stat-performers').textContent = data.stash.performers.toLocaleString();
                    document.getElementById('stat-studios').textContent = data.stash.studios.toLocaleString();
                    document.getElementById('stat-tags').textContent = data.stash.tags.toLocaleString();
                    document.getElementById('stat-groups').textContent = data.stash.groups.toLocaleString();
                }

                // Update Proxy usage stats
                if (data.proxy) {
                    document.getElementById('stat-streams-today').textContent = data.proxy.streams_today.toLocaleString();
                    document.getElementById('stat-total-streams').textContent = data.proxy.total_streams.toLocaleString();
                    document.getElementById('stat-unique-ips').textContent = data.proxy.unique_ips_today.toLocaleString();
                    document.getElementById('stat-auth-success').textContent = data.proxy.auth_success.toLocaleString();
                    document.getElementById('stat-auth-failed').textContent = data.proxy.auth_failed.toLocaleString();

                    // Update top played list
                    const topList = document.getElementById('top-played-list');
                    if (data.proxy.top_played && data.proxy.top_played.length > 0) {
                        topList.innerHTML = data.proxy.top_played.map((item, idx) => `
                            <div class="top-played-item">
                                <span class="top-played-rank">${idx + 1}</span>
                                <div class="top-played-info">
                                    <div class="top-played-title">${item.title}</div>
                                    <div class="top-played-performer">${item.performer || 'Unknown'}</div>
                                </div>
                                <span class="top-played-count">${item.count}x</span>
                            </div>
                        `).join('');
                    } else {
                        topList.innerHTML = '<div class="empty-state">No play data yet</div>';
                    }
                }
            } catch (e) {
                console.error('Failed to fetch stats:', e);
            }
        }

        // Default values - if field matches default, show placeholder instead
        const DEFAULTS = {
            STASH_URL: '',
            STASH_API_KEY: '',
            STASH_GRAPHQL_PATH: '/graphql',
            STASH_VERIFY_TLS: false,
            PROXY_BIND: '0.0.0.0',
            PROXY_PORT: 8096,
            UI_PORT: 8097,
            SJS_USER: '',
            SJS_PASSWORD: '',
            SERVER_ID: '',
            SERVER_NAME: 'Stash Media Server',
            TAG_GROUPS: [],
            LATEST_GROUPS: ['Scenes'],
            STASH_TIMEOUT: 30,
            STASH_RETRIES: 3,
            ENABLE_FILTERS: true,
            ENABLE_IMAGE_RESIZE: true,
            ENABLE_TAG_FILTERS: false,
            ENABLE_ALL_TAGS: false,
            REQUIRE_AUTH_FOR_CONFIG: false,
            IMAGE_CACHE_MAX_SIZE: 1000,
            DEFAULT_PAGE_SIZE: 50,
            MAX_PAGE_SIZE: 200,
            LOG_LEVEL: 'INFO',
            LOG_DIR: '/config',
            LOG_FILE: 'stash_jellyfin_proxy.log',
            LOG_MAX_SIZE_MB: 10,
            LOG_BACKUP_COUNT: 3,
            BAN_THRESHOLD: 10,
            BAN_WINDOW_MINUTES: 15,
            BANNED_IPS: ''
        };

        async function fetchConfig() {
            try {
                const res = await fetch('/api/config');
                const data = await res.json();
                state.config = data.config || data;
                const envFields = data.env_fields || [];
                const definedFields = data.defined_fields || [];

                // Show env fields notice if any exist
                const envNotice = document.getElementById('env-notice');
                if (envFields.length > 0) {
                    envNotice.style.display = 'block';
                    envNotice.querySelector('span').textContent = envFields.join(', ');
                } else {
                    envNotice.style.display = 'none';
                }

                Object.entries(state.config).forEach(([key, value]) => {
                    const input = document.querySelector(`[name="${key}"]`);
                    if (input) {
                        const defaultVal = DEFAULTS[key];
                        const isEnvField = envFields.includes(key);
                        const isDefinedInConfig = definedFields.includes(key);

                        // Mark env fields as read-only with visual indicator
                        if (isEnvField) {
                            if (input.type === 'checkbox') {
                                // For checkboxes, disable and style the label
                                input.disabled = true;
                                const label = input.closest('label');
                                if (label) label.classList.add('env-locked-label');
                            } else {
                                input.readOnly = true;
                                input.disabled = input.tagName === 'SELECT';
                                input.classList.add('env-locked');
                            }
                        }

                        if (input.type === 'checkbox') {
                            input.checked = value === true || value === 'true';
                        } else if (input.tagName === 'SELECT') {
                            // Always set select value (dropdowns should show selection)
                            input.value = value;
                        } else if (Array.isArray(value)) {
                            const valStr = value.join(', ');
                            const defStr = Array.isArray(defaultVal) ? defaultVal.join(', ') : '';
                            // Show value if different from default OR explicitly defined in config
                            input.value = (valStr !== defStr || isDefinedInConfig) ? valStr : '';
                        } else {
                            // Show value if different from default OR explicitly defined in config
                            const strVal = String(value);
                            const strDef = String(defaultVal ?? '');
                            input.value = (strVal !== strDef || isDefinedInConfig) ? value : '';
                        }
                    }
                });
            } catch (e) {
                console.error('Failed to fetch config:', e);
            }
        }

        // Normalize path: ensure leading /, remove trailing /
        function normalizePath(path) {
            if (!path || path.trim() === '') return '/graphql';
            let p = path.trim();
            if (!p.startsWith('/')) p = '/' + p;
            if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
            return p;
        }

        // Form submission
        document.getElementById('config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const config = {};
            const intFields = ['PROXY_PORT', 'UI_PORT', 'STASH_TIMEOUT', 'STASH_RETRIES', 'LOG_MAX_SIZE_MB', 'LOG_BACKUP_COUNT', 'DEFAULT_PAGE_SIZE', 'MAX_PAGE_SIZE', 'IMAGE_CACHE_MAX_SIZE', 'BAN_THRESHOLD', 'BAN_WINDOW_MINUTES'];
            const boolFields = ['ENABLE_FILTERS', 'ENABLE_IMAGE_RESIZE', 'ENABLE_TAG_FILTERS', 'ENABLE_ALL_TAGS', 'REQUIRE_AUTH_FOR_CONFIG', 'STASH_VERIFY_TLS'];

            formData.forEach((value, key) => {
                // If field is empty, use the default value
                const defaultVal = DEFAULTS[key];
                if (key === 'TAG_GROUPS' || key === 'LATEST_GROUPS') {
                    if (value.trim() === '' && Array.isArray(defaultVal)) {
                        config[key] = defaultVal;
                    } else {
                        config[key] = value.split(',').map(s => s.trim()).filter(Boolean);
                    }
                } else if (key === 'STASH_GRAPHQL_PATH') {
                    // Normalize GraphQL path: ensure leading /, remove trailing /
                    config[key] = normalizePath(value);
                } else if (intFields.includes(key)) {
                    config[key] = value.trim() === '' ? defaultVal : (parseInt(value) || 0);
                } else {
                    config[key] = value.trim() === '' ? (defaultVal ?? '') : value;
                }
            });

            // Handle checkboxes (not included in FormData if unchecked)
            boolFields.forEach(key => {
                const checkbox = document.querySelector(`[name="${key}"]`);
                if (checkbox) {
                    config[key] = checkbox.checked;
                }
            });
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(config)
                });
                if (res.ok) {
                    const result = await res.json();
                    if (result.needs_restart && result.needs_restart.length > 0) {
                        showToast(`Configuration saved. ${result.needs_restart.join(', ')} require restart.`, 'warning');
                    } else if (result.applied_immediately && result.applied_immediately.length > 0) {
                        showToast('Configuration saved and applied!', 'success');
                    } else {
                        showToast('Configuration saved.', 'success');
                    }
                    // Refresh config to reflect new values
                    fetchConfig();
                } else {
                    showToast('Failed to save configuration', 'error');
                }
            } catch (e) {
                showToast('Failed to save configuration', 'error');
            }
        });

        // Restart button
        document.getElementById('restart-btn').addEventListener('click', async () => {
            if (!confirm('Are you sure you want to restart the server? Active streams will be interrupted.')) {
                return;
            }
            try {
                showToast('Restarting server...', 'info');
                const res = await fetch('/api/restart', { method: 'POST' });
                if (res.ok) {
                    // Poll for server to come back up
                    let attempts = 0;
                    const maxAttempts = 30;
                    const checkServer = async () => {
                        attempts++;
                        try {
                            const statusRes = await fetch('/api/status', { cache: 'no-store' });
                            if (statusRes.ok) {
                                showToast('Server restarted successfully!', 'success');
                                setTimeout(() => location.reload(), 1000);
                                return;
                            }
                        } catch (e) {}
                        if (attempts < maxAttempts) {
                            setTimeout(checkServer, 1000);
                        } else {
                            showToast('Server restart timed out. Please refresh manually.', 'error');
                        }
                    };
                    setTimeout(checkServer, 2000);
                } else {
                    showToast('Failed to restart server', 'error');
                }
            } catch (e) {
                // Expected - server is restarting
                setTimeout(() => location.reload(), 3000);
            }
        });

        // Log filters
        document.getElementById('log-level-filter').addEventListener('change', renderLogs);
        document.getElementById('log-search').addEventListener('input', renderLogs);

        // Download logs
        document.getElementById('download-logs').addEventListener('click', async () => {
            try {
                const res = await fetch('/api/logs?limit=10000');
                const data = await res.json();
                const text = data.entries.map(l => `${l.timestamp} [${l.level}] ${l.message}`).join('\\n');
                const blob = new Blob([text], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'stash_jellyfin_proxy.log';
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                showToast('Failed to download logs', 'error');
            }
        });

        // Reset statistics
        document.getElementById('reset-stats-btn').addEventListener('click', async () => {
            if (!confirm('Reset all usage statistics? This will clear stream counts, play history, and auth stats.')) {
                return;
            }
            try {
                const res = await fetch('/api/stats/reset', { method: 'POST' });
                if (res.ok) {
                    showToast('Statistics reset successfully', 'success');
                    fetchStats();
                } else {
                    showToast('Failed to reset statistics', 'error');
                }
            } catch (e) {
                showToast('Failed to reset statistics', 'error');
            }
        });

        function showToast(message, type) {
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            toast.textContent = message;
            document.body.appendChild(toast);
            setTimeout(() => toast.remove(), 3000);
        }

        // Polling
        async function poll() {
            if (state.currentPage === 'dashboard') {
                await Promise.all([fetchStatus(), fetchStreams(), fetchLogs(), fetchStats()]);
            } else if (state.currentPage === 'logs') {
                await fetchLogs();
            }
        }

        // Initial load
        fetchStatus();
        fetchStreams();
        fetchLogs();
        fetchStats();
        fetchConfig();
        setInterval(poll, 5000);
    </script>
</body>
</html>'''

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

    # Console handler (always enabled)
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

            # Set up rotating file handler
            if LOG_MAX_SIZE_MB > 0:
                max_bytes = LOG_MAX_SIZE_MB * 1024 * 1024
                file_handler = RotatingFileHandler(
                    log_path,
                    maxBytes=max_bytes,
                    backupCount=LOG_BACKUP_COUNT
                )
            else:
                file_handler = logging.FileHandler(log_path)

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
# Track active streams to detect start/resume/stop
# scene_id -> {"last_seen": timestamp, "started": timestamp, "title": str, "user": str, "client_ip": str, "client_type": str, "client_key": str}
_active_streams = {}
# Track which client is watching which scene (for single-stream-per-client enforcement)
# client_key -> scene_id
_client_streams = {}
# Track recently stopped streams to prevent false "started" messages after stop
# scene_id -> timestamp when stopped
_recently_stopped = {}
STREAM_RESUME_THRESHOLD = 90  # seconds of inactivity before considering it a "resume" (Infuse buffers ~60s)
RECENTLY_STOPPED_GRACE = 5  # seconds to ignore new stream requests after a stop (prevents stop/start race)

# Play count cooldown - prevents double-counting rapid start/stop cycles
# Cooldown = video duration + buffer time
PLAY_COOLDOWN_BUFFER = 1800  # 30 minutes buffer on top of video duration
# In-memory tracking: (scene_id, client_ip) -> {"timestamp": float, "cooldown_seconds": float}
_play_cooldowns = {}

# Smart stream counting - track position to detect intentional new plays vs seeking
# Constants for stream counting thresholds
STREAM_COUNT_COOLDOWN = 1800  # 30 minutes - always count as new stream after this gap
STREAM_START_GAP = 300  # 5 minutes - minimum gap needed for "seek to start" to count
STREAM_START_THRESHOLD = 0.05  # 5% - seeking to first 5% of file considered "start"
# In-memory tracking: (scene_id, client_ip) -> {"last_position": int, "last_time": float, "file_size": int}
_stream_positions = {}

# --- Proxy Statistics Tracking ---
# Stats are persisted to JSON file and survive restarts
STATS_FILE = os.path.join(os.path.dirname(CONFIG_FILE) if CONFIG_FILE else ".", "proxy_stats.json")
_proxy_stats = {
    "total_streams": 0,           # Lifetime total streams
    "streams_today": 0,           # Streams started today
    "streams_today_date": "",     # Date string for today's count (YYYY-MM-DD)
    "unique_ips_today": [],       # List of unique IPs that connected today
    "auth_success": 0,            # Lifetime successful auths
    "auth_failed": 0,             # Lifetime failed auths
    "play_counts": {},            # scene_id -> {"count": int, "title": str, "performer": str, "last_played": timestamp}
}
_stats_dirty = False  # Flag to track if stats need saving
_stats_last_save = 0  # Timestamp of last save

def load_proxy_stats():
    """Load stats from JSON file."""
    global _proxy_stats
    if os.path.isfile(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                loaded = json.load(f)
                # Merge with defaults to handle missing keys
                for key in _proxy_stats:
                    if key in loaded:
                        _proxy_stats[key] = loaded[key]
            logger.debug(f"Loaded proxy stats from {STATS_FILE}")
        except Exception as e:
            logger.warning(f"Could not load proxy stats: {e}")

def save_proxy_stats():
    """Save stats to JSON file."""
    global _stats_dirty, _stats_last_save
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(_proxy_stats, f, indent=2)
        _stats_dirty = False
        _stats_last_save = time.time()
        logger.debug(f"Saved proxy stats to {STATS_FILE}")
    except Exception as e:
        logger.warning(f"Could not save proxy stats: {e}")

def maybe_save_stats():
    """Save stats if dirty and enough time has passed (every 60 seconds)."""
    global _stats_dirty
    if _stats_dirty and (time.time() - _stats_last_save) > 60:
        save_proxy_stats()

def reset_daily_stats_if_needed():
    """Reset daily counters if the date has changed."""
    today = time.strftime("%Y-%m-%d")
    if _proxy_stats["streams_today_date"] != today:
        _proxy_stats["streams_today"] = 0
        _proxy_stats["streams_today_date"] = today
        _proxy_stats["unique_ips_today"] = []

def should_count_as_new_stream(scene_id: str, client_ip: str, byte_position: int, file_size: int) -> tuple:
    """Determine if this stream request should count as a new stream.

    Uses smart detection based on playback position:
    - 30+ min since last activity → always counts as new stream
    - Seek to start (first 5%) with 5+ min gap → counts as new stream
    - First request at start of file → counts as new stream
    - First request mid-file → likely trailing request after restart, DON'T count
    - Otherwise (seeking within video) → doesn't count

    Returns tuple of (should_count: bool, is_trailing_after_restart: bool).
    is_trailing_after_restart indicates this appears to be a post-restart trailing request.
    """
    position_key = (scene_id, client_ip)
    now = time.time()

    # First time seeing this scene from this client
    if position_key not in _stream_positions:
        _stream_positions[position_key] = {
            "last_position": byte_position,
            "last_time": now,
            "file_size": file_size
        }
        # Only count if starting from beginning of file
        # If position is mid-file (or unknown size but non-zero position), this is likely a trailing request after server restart
        if file_size > 0:
            position_ratio = byte_position / file_size
            if position_ratio > STREAM_START_THRESHOLD:
                logger.debug(f"Ignoring mid-file first request for {scene_id}: position {position_ratio:.1%} (likely post-restart trailing request)")
                return (False, True)  # Don't count, IS trailing after restart
        elif byte_position > 0:
            # Unknown file size but non-zero position - likely trailing request
            logger.debug(f"Ignoring non-zero first request for {scene_id}: position {byte_position} bytes, unknown size (likely post-restart)")
            return (False, True)  # Don't count, IS trailing after restart
        return (True, False)  # Count, NOT trailing after restart

    last_info = _stream_positions[position_key]
    elapsed = now - last_info["last_time"]

    # Update position tracking
    _stream_positions[position_key] = {
        "last_position": byte_position,
        "last_time": now,
        "file_size": file_size or last_info["file_size"]
    }

    # Check 1: 30+ minute gap - definitely a new stream
    if elapsed >= STREAM_COUNT_COOLDOWN:
        logger.debug(f"New stream counted for {scene_id}: {int(elapsed/60)}min gap exceeds cooldown")
        return (True, False)

    # Check 2: Seek to start with sufficient gap
    effective_file_size = file_size or last_info["file_size"]
    if effective_file_size > 0:
        position_ratio = byte_position / effective_file_size
        is_at_start = position_ratio <= STREAM_START_THRESHOLD
        has_sufficient_gap = elapsed >= STREAM_START_GAP

        if is_at_start and has_sufficient_gap:
            logger.debug(f"New stream counted for {scene_id}: seek to start ({position_ratio:.1%}) with {int(elapsed/60)}min gap")
            return (True, False)
        elif is_at_start:
            logger.debug(f"Seek to start ignored for {scene_id}: only {int(elapsed)}s gap (need {STREAM_START_GAP}s)")

    # Otherwise, this is just seeking/buffering within the same session
    logger.debug(f"Same stream session for {scene_id}: position {byte_position}, {int(elapsed)}s since last")
    return (False, False)

def record_play_count(scene_id: str, title: str, performer: str, client_ip: str, duration: float = 0):
    """Record a play count for the Top Played list with duration-based cooldown.

    Args:
        scene_id: The scene identifier
        title: Scene title for display
        performer: Performer name(s)
        client_ip: Client IP address
        duration: Video duration in seconds (for cooldown calculation)

    Play counts use duration-based cooldown (duration + 30 min buffer) to count unique views.
    Stream counts are handled separately by should_count_as_new_stream().
    """
    global _stats_dirty

    # Ensure duration is a valid positive number (guard against None/negative)
    safe_duration = max(0, float(duration or 0))
    cooldown_key = (scene_id, client_ip)
    cooldown_seconds = safe_duration + PLAY_COOLDOWN_BUFFER
    now = time.time()

    should_count_play = True
    if cooldown_key in _play_cooldowns:
        last_play = _play_cooldowns[cooldown_key]
        elapsed = now - last_play["timestamp"]
        if elapsed < last_play["cooldown_seconds"]:
            # Still in cooldown - don't count this play
            should_count_play = False
            remaining = int(last_play["cooldown_seconds"] - elapsed)
            logger.debug(f"Play cooldown active for {scene_id} from {client_ip} ({remaining}s remaining)")

    if should_count_play:
        # Update cooldown tracking
        _play_cooldowns[cooldown_key] = {
            "timestamp": now,
            "cooldown_seconds": cooldown_seconds
        }

        # Update play count for this scene
        if scene_id not in _proxy_stats["play_counts"]:
            _proxy_stats["play_counts"][scene_id] = {
                "count": 0,
                "title": title,
                "performer": performer,
                "last_played": 0
            }

        _proxy_stats["play_counts"][scene_id]["count"] += 1
        _proxy_stats["play_counts"][scene_id]["title"] = title  # Update in case it changed
        _proxy_stats["play_counts"][scene_id]["performer"] = performer
        _proxy_stats["play_counts"][scene_id]["last_played"] = now

        # Log the cooldown duration for debugging
        cooldown_mins = int(cooldown_seconds / 60)
        logger.debug(f"Play counted for {scene_id} from {client_ip} (cooldown: {cooldown_mins}min)")

    _stats_dirty = True
    maybe_save_stats()

def record_auth_attempt(success: bool):
    """Record an authentication attempt."""
    global _stats_dirty
    if success:
        _proxy_stats["auth_success"] += 1
    else:
        _proxy_stats["auth_failed"] += 1
    _stats_dirty = True

def get_top_played_scenes(limit: int = 5) -> list:
    """Get the top N most played scenes."""
    play_counts = _proxy_stats.get("play_counts", {})
    sorted_scenes = sorted(
        play_counts.items(),
        key=lambda x: x[1].get("count", 0),
        reverse=True
    )[:limit]

    return [
        {
            "scene_id": scene_id,
            "title": info.get("title", scene_id),
            "performer": info.get("performer", ""),
            "count": info.get("count", 0)
        }
        for scene_id, info in sorted_scenes
    ]

def get_proxy_stats() -> dict:
    """Get current proxy statistics."""
    reset_daily_stats_if_needed()
    return {
        "total_streams": _proxy_stats["total_streams"],
        "streams_today": _proxy_stats["streams_today"],
        "unique_ips_today": len(_proxy_stats["unique_ips_today"]),
        "auth_success": _proxy_stats["auth_success"],
        "auth_failed": _proxy_stats["auth_failed"],
        "top_played": get_top_played_scenes(5)
    }

def get_scene_title(scene_id: str) -> str:
    """Fetch scene title from Stash for logging."""
    info = get_scene_info(scene_id)
    return info.get("title", scene_id)

def get_scene_info(scene_id: str) -> dict:
    """Fetch scene title, performer, duration, and file size from Stash."""
    try:
        numeric_id = scene_id.replace("scene-", "")
        query = """query($id: ID!) {
            findScene(id: $id) {
                title
                files { basename duration size }
                performers { name }
            }
        }"""
        result = stash_query(query, {"id": numeric_id})
        scene = result.get("data", {}).get("findScene")
        if scene:
            title = scene.get("title")
            duration = 0
            file_size = 0
            files = scene.get("files", [])
            if files:
                if not title and files[0].get("basename"):
                    title = files[0]["basename"]
                # Get duration from first file (in seconds)
                duration = files[0].get("duration", 0) or 0
                # Get file size in bytes
                file_size = files[0].get("size", 0) or 0
            if not title:
                title = scene_id

            # Get performer name(s)
            performers = scene.get("performers", [])
            performer = performers[0]["name"] if performers else ""
            if len(performers) > 1:
                performer = f"{performer} +{len(performers)-1}"

            return {"title": title, "performer": performer, "duration": duration, "file_size": file_size}
    except:
        pass
    return {"title": scene_id, "performer": "", "duration": 0, "file_size": 0}

def mark_stream_stopped(scene_id: str, from_stop_notification: bool = False):
    """Mark a stream as stopped so next request shows as 'started'."""
    if scene_id in _active_streams:
        stream_info = _active_streams[scene_id]
        client_key = stream_info.get("client_key")
        # Remove from client tracking
        if client_key and _client_streams.get(client_key) == scene_id:
            del _client_streams[client_key]
        del _active_streams[scene_id]

    # If this came from a stop notification, add to recently stopped to prevent false re-start
    if from_stop_notification:
        _recently_stopped[scene_id] = time.time()
        # Clean up old entries (older than grace period)
        now = time.time()
        expired = [k for k, v in _recently_stopped.items() if now - v > RECENTLY_STOPPED_GRACE * 2]
        for k in expired:
            del _recently_stopped[k]

def cancel_client_streams(client_key: str, new_scene_id: str = None) -> list:
    """Cancel any existing streams from this client (except new_scene_id). Returns list of cancelled scene_ids."""
    cancelled = []
    current_scene = _client_streams.get(client_key)
    if current_scene and current_scene != new_scene_id:
        # Client is starting a different video - cancel the old one
        if current_scene in _active_streams:
            old_info = _active_streams[current_scene]
            logger.info(f"⏹ Stream cancelled: {old_info.get('title', current_scene)} ({current_scene}) - client started new video")
            del _active_streams[current_scene]
            cancelled.append(current_scene)
        del _client_streams[client_key]
    return cancelled

# Endpoints that don't require authentication (for client discovery)
PUBLIC_ENDPOINTS = {
    "/",
    "/favicon.ico",  # Browser requests this automatically
    "/System/Info/Public",
    "/System/Info",
    "/System/Ping",
    "/Users",  # User list for login screen
    "/Users/AuthenticateByName",
    "/Branding/Configuration",
}

# Endpoint prefixes that don't require auth (for discovery/public info)
PUBLIC_PREFIXES = [
    "/System/Info",
]

# IP failure tracking: {ip: [(timestamp, path), ...]}
_ip_failures = {}
_ip_failures_lock = False  # Simple flag to avoid concurrent modification issues

def get_client_ip(scope) -> str:
    """Extract client IP from request, checking X-Forwarded-For for proxied requests."""
    headers = {}
    for key, value in scope.get("headers", []):
        headers[key.decode().lower()] = value.decode()

    # Check X-Forwarded-For first (set by reverse proxies like SWAG/nginx)
    xff = headers.get("x-forwarded-for", "")
    if xff:
        # Take the first IP (original client)
        return xff.split(",")[0].strip()

    # Check X-Real-IP (alternative header)
    xri = headers.get("x-real-ip", "")
    if xri:
        return xri.strip()

    # Fall back to direct connection
    client = scope.get("client", ("unknown", 0))
    return client[0] if client else "unknown"

def record_auth_failure(client_ip: str, path: str, reason: str, user_agent: str = ""):
    """Record a failed auth attempt and check if IP should be banned.

    Rate limits failure counting to 1 per second per IP to avoid instant bans
    from parallel requests (e.g., Infuse makes many requests on startup).
    """
    global BANNED_IPS, _ip_failures

    now = time.time()
    window_seconds = BAN_WINDOW_MINUTES * 60

    # Clean up old entries for this IP
    if client_ip in _ip_failures:
        _ip_failures[client_ip] = [
            (ts, p) for ts, p in _ip_failures[client_ip]
            if now - ts < window_seconds
        ]
    else:
        _ip_failures[client_ip] = []

    # Rate limit: only count 1 failure per second to avoid instant bans from parallel requests
    # Infuse can make 10+ parallel requests on startup, all with stale tokens
    recent_failures = _ip_failures[client_ip]
    if recent_failures:
        last_failure_time = recent_failures[-1][0]
        if now - last_failure_time < 1.0:
            # Already recorded a failure within the last second, skip counting this one
            logger.debug(f"🚫 Auth failed (rate limited): {client_ip} -> {path} ({reason})")
            return

    # Add this failure
    _ip_failures[client_ip].append((now, path))

    failure_count = len(_ip_failures[client_ip])

    # Log the failure - first 2 attempts at DEBUG (usually stale tokens from legit clients)
    # After that, escalate to WARNING as it may indicate an attack
    ua_info = f", UA: {user_agent[:50]}" if user_agent else ""
    log_msg = f"🚫 Auth failed: {client_ip} -> {path} ({reason}) [attempt {failure_count}/{BAN_THRESHOLD}]{ua_info}"
    if failure_count <= 2:
        logger.debug(log_msg)
    else:
        logger.warning(log_msg)

    # Check if threshold exceeded
    if failure_count >= BAN_THRESHOLD:
        # Ban this IP
        BANNED_IPS.add(client_ip)
        logger.warning(f"🔒 IP BANNED: {client_ip} (exceeded {BAN_THRESHOLD} failures in {BAN_WINDOW_MINUTES} minutes)")

        # Persist to config file
        save_banned_ips_to_config()

        # Clear the failure tracking for this IP
        del _ip_failures[client_ip]

def save_banned_ips_to_config():
    """Save the current BANNED_IPS set to the config file."""
    global BANNED_IPS

    if not os.path.isfile(CONFIG_FILE):
        return

    banned_str = ", ".join(sorted(BANNED_IPS)) if BANNED_IPS else ""

    try:
        with open(CONFIG_FILE, 'r') as f:
            lines = f.readlines()

        # Find and update BANNED_IPS line, or add it
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('BANNED_IPS') and '=' in stripped:
                new_lines.append(f'BANNED_IPS = "{banned_str}"\n')
                found = True
            elif stripped.startswith('#') and 'BANNED_IPS' in stripped and '=' in stripped:
                # Uncomment and update
                new_lines.append(f'BANNED_IPS = "{banned_str}"\n')
                found = True
            else:
                new_lines.append(line)

        # If not found, add at end
        if not found:
            new_lines.append(f'\n# Auto-generated by IP ban system\nBANNED_IPS = "{banned_str}"\n')

        with open(CONFIG_FILE, 'w') as f:
            f.writelines(new_lines)

        logger.info(f"Saved banned IPs to config: {banned_str if banned_str else '(none)'}")
    except Exception as e:
        logger.error(f"Failed to save banned IPs to config: {e}")

class AuthenticationMiddleware:
    """ASGI middleware that validates ACCESS_TOKEN on protected endpoints and enforces IP bans."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        client_ip = get_client_ip(scope)

        # Get user agent for logging
        user_agent = ""
        for key, value in scope.get("headers", []):
            if key.decode().lower() == "user-agent":
                user_agent = value.decode()
                break

        # Check if IP is banned FIRST (before any other processing)
        # Silent drop - don't respond at all, forcing client timeout
        # This doesn't confirm to attackers that their IP is banned
        if client_ip in BANNED_IPS:
            logger.debug(f"🚫 Silent drop for banned IP: {client_ip} -> {path}")
            # Simply return without sending any response - connection will timeout
            return

        # Check if this is a public endpoint
        is_public = path in PUBLIC_ENDPOINTS
        if not is_public:
            for prefix in PUBLIC_PREFIXES:
                if path.startswith(prefix):
                    is_public = True
                    break

        # Allow public endpoints without auth
        if is_public:
            await self.app(scope, receive, send)
            return

        # Extract token from headers
        token = None
        for key, value in scope.get("headers", []):
            key_lower = key.decode().lower()
            value_str = value.decode()

            # Check X-Emby-Token header (Jellyfin clients)
            if key_lower == "x-emby-token":
                token = value_str
                break
            # Check X-MediaBrowser-Token header (older clients)
            elif key_lower == "x-mediabrowser-token":
                token = value_str
                break
            # Check Authorization header (Bearer token or X-Emby-Authorization)
            elif key_lower == "authorization":
                if value_str.startswith("Bearer "):
                    token = value_str[7:]
                elif "Token=" in value_str:
                    # Parse X-Emby-Authorization format: MediaBrowser Client="...", Token="..."
                    match = re.search(r'Token="([^"]+)"', value_str)
                    if match:
                        token = match.group(1)
                break
            # Check X-Emby-Authorization header
            elif key_lower == "x-emby-authorization":
                match = re.search(r'Token="([^"]+)"', value_str)
                if match:
                    token = match.group(1)
                break

        # Validate token
        if token and token == ACCESS_TOKEN:
            # Valid token - proceed
            await self.app(scope, receive, send)
            return

        # No valid token - record failure and return 401
        reason = "invalid token" if token else "missing token"
        record_auth_failure(client_ip, path, reason, user_agent)

        response_body = b'{"error": "Unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(response_body)).encode()],
            ],
        })
        await send({
            "type": "http.response.body",
            "body": response_body,
        })


class RequestLoggingMiddleware:
    """Pure ASGI middleware that doesn't wrap streaming responses (avoids BaseHTTPMiddleware issues)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        path = scope.get("path", "")
        client = scope.get("client", ("unknown", 0))
        client_host = client[0] if client else "unknown"

        # Get headers dict for logging
        headers = {}
        for key, value in scope.get("headers", []):
            headers[key.decode().lower()] = value.decode()

        # Track response status
        response_status = [0]  # Use list to allow mutation in nested function

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status[0] = message.get("status", 0)
            try:
                await send(message)
            except Exception:
                # Ignore send errors (client disconnected)
                pass

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            # Check if this is a client disconnect error (expected for streaming)
            error_str = str(e).lower()
            if "content-length" in error_str or "disconnect" in error_str or "cancelled" in error_str:
                # Client disconnected during streaming - this is normal for video seeking
                pass
            else:
                process_time = time.time() - start_time
                ms = int(process_time * 1000)
                logger.error(f"{path} -> ERROR ({ms}ms): {str(e)}")
                return

        # Log the request
        process_time = time.time() - start_time
        ms = int(process_time * 1000)
        status = response_status[0]

        # Determine log level based on request type and result
        is_error = status >= 400
        is_auth = "/Authenticate" in path
        is_stream = "/stream" in path.lower() or "/Videos/" in path
        is_slow = ms > 1000

        if is_error and status > 0:
            logger.warning(f"{path} -> {status} ({ms}ms)")
        elif is_auth:
            logger.debug(f"Login request completed -> {status} ({ms}ms)")
        elif is_stream:
            # Extract scene ID from path like /Videos/scene-35734/stream
            match = re.search(r'/(scene-\d+)/', path)
            scene_id = match.group(1) if match else "unknown"
            now = time.time()

            # Extract user from path (e.g., /Users/billy/...), fallback to login username
            user_match = re.search(r'/Users/([^/]+)/', path)
            user = user_match.group(1) if user_match else SJS_USER

            # Get client info from headers
            client_ip = headers.get("x-forwarded-for", client_host).split(",")[0].strip()
            user_agent = headers.get("user-agent", "")
            # Parse client type from User-Agent (Infuse, VLC, etc.)
            if "Infuse" in user_agent:
                client_type = "Infuse"
            elif "VLC" in user_agent:
                client_type = "VLC"
            elif "Jellyfin" in user_agent:
                client_type = "Jellyfin"
            else:
                client_type = user_agent.split("/")[0][:20] if user_agent else "Unknown"

            # Extract byte position from Range header (e.g., "bytes=12345-" or "bytes=12345-67890")
            range_header = headers.get("range", "")
            byte_position = 0
            if range_header.startswith("bytes="):
                try:
                    range_spec = range_header[6:]  # Remove "bytes="
                    byte_position = int(range_spec.split("-")[0])
                except (ValueError, IndexError):
                    pass

            # Create a unique client key (IP + client type)
            client_key = f"{client_ip}|{client_type}"

            # Check if this is a new stream, resume, or continuation
            stream_info = _active_streams.get(scene_id)

            # Get scene info for file_size (needed for position-based counting)
            cached_file_size = stream_info.get("file_size", 0) if stream_info else 0
            if not cached_file_size:
                scene_info = get_scene_info(scene_id)
                cached_file_size = scene_info.get("file_size", 0)

            # Smart stream counting: check on EVERY request if this should count as new stream
            # This runs independently of _active_streams tracking (which is for UI display)
            count_result = should_count_as_new_stream(scene_id, client_ip, byte_position, cached_file_size)
            should_count, is_trailing_after_restart = count_result

            if should_count:
                reset_daily_stats_if_needed()
                _proxy_stats["total_streams"] += 1
                _proxy_stats["streams_today"] += 1
                # Track unique IPs
                if client_ip not in _proxy_stats["unique_ips_today"]:
                    _proxy_stats["unique_ips_today"].append(client_ip)
                global _stats_dirty
                _stats_dirty = True
                maybe_save_stats()

            # Check if stream_info has expired (gap > 30 min = treat as new for UI purposes)
            if stream_info and (now - stream_info["last_seen"]) >= STREAM_COUNT_COOLDOWN:
                logger.debug(f"Stream expired for {scene_id}: {int((now - stream_info['last_seen'])/60)}min gap")
                # Mark as stopped and clear
                mark_stream_stopped(scene_id, from_stop_notification=False)
                stream_info = None

            if stream_info is None:
                # Check if this stream was recently stopped (prevents false start after stop notification)
                stopped_at = _recently_stopped.get(scene_id)
                if stopped_at and (now - stopped_at) < RECENTLY_STOPPED_GRACE:
                    # This is a trailing request after a stop - ignore it
                    logger.debug(f"Ignoring trailing request for recently stopped stream: {scene_id}")
                elif is_trailing_after_restart:
                    # This is a trailing request after server restart - track for UI but don't log as "started"
                    scene_info = get_scene_info(scene_id)
                    title = scene_info.get("title", scene_id)
                    _active_streams[scene_id] = {
                        "last_seen": now,
                        "started": now,
                        "title": title,
                        "performer": scene_info.get("performer", ""),
                        "user": user,
                        "client_ip": client_ip,
                        "client_type": client_type,
                        "client_key": client_key,
                        "file_size": cached_file_size
                    }
                    _client_streams[client_key] = scene_id
                    logger.info(f"⏸ Stream resuming (post-restart): {title} ({scene_id}) from {client_ip}")
                else:
                    # New stream for this scene - check if client is switching from another video
                    cancel_client_streams(client_key, scene_id)

                    # Clear from recently stopped if present
                    if scene_id in _recently_stopped:
                        del _recently_stopped[scene_id]

                    # Now start tracking the new stream (for UI display)
                    scene_info = get_scene_info(scene_id)
                    title = scene_info.get("title", scene_id)
                    performer = scene_info.get("performer", "")
                    duration = scene_info.get("duration", 0)
                    file_size = scene_info.get("file_size", 0)
                    _active_streams[scene_id] = {
                        "last_seen": now,
                        "started": now,
                        "title": title,
                        "performer": performer,
                        "user": user,
                        "client_ip": client_ip,
                        "client_type": client_type,
                        "client_key": client_key,
                        "file_size": file_size
                    }
                    _client_streams[client_key] = scene_id
                    # Record play count (with duration-based cooldown, separate from stream counting)
                    record_play_count(scene_id, title, performer, client_ip, duration)
                    logger.info(f"▶ Stream started: {title} ({scene_id}) by {user} from {client_ip} [{client_type}]")
            elif (now - stream_info["last_seen"]) > STREAM_RESUME_THRESHOLD:
                # Gap in activity = resumed after pause
                gap = int(now - stream_info["last_seen"])
                stream_info["last_seen"] = now
                logger.info(f"▶ Stream resumed: {stream_info['title']} ({scene_id}, paused {gap}s)")
            else:
                # Continuous playback - just update timestamp
                stream_info["last_seen"] = now
                logger.debug(f"Stream continue: {scene_id} ({ms}ms)")
        elif is_slow:
            logger.info(f"Slow request: {path} ({ms}ms)")
        elif status > 0:
            logger.debug(f"{path} -> {status} ({ms}ms)")

# --- Stash GraphQL Client ---
# Construct GraphQL URL from base URL and path
GRAPHQL_URL = f"{STASH_URL.rstrip('/')}{STASH_GRAPHQL_PATH}"

# Create a persistent session with authentication
STASH_SESSION = None
STASH_VERSION = ""  # Populated after successful connection
STASH_CONNECTED = False  # Track connection status

def get_stash_session():
    """Get or create a Stash session with ApiKey authentication."""
    global STASH_SESSION

    if STASH_SESSION is not None:
        return STASH_SESSION

    STASH_SESSION = requests.Session()

    # Set TLS verification (can be disabled for self-signed certs in Docker)
    STASH_SESSION.verify = STASH_VERIFY_TLS
    if not STASH_VERIFY_TLS:
        # Suppress InsecureRequestWarning when TLS verification is disabled
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.info("TLS verification disabled (STASH_VERIFY_TLS=false)")

    # Use STASH_API_KEY for authentication (required for image endpoints)
    if STASH_API_KEY:
        STASH_SESSION.headers["ApiKey"] = STASH_API_KEY
        logger.info(f"Session configured with ApiKey header (key length: {len(STASH_API_KEY)})")
    else:
        logger.warning("No STASH_API_KEY configured - images will fail to load!")
        logger.warning("Add STASH_API_KEY to your config file (get from Stash -> Settings -> Security)")

    return STASH_SESSION

def check_stash_connection():
    """Verify we can talk to Stash at startup."""
    global STASH_VERSION, STASH_CONNECTED
    try:
        logger.info(f"Testing connection to Stash at {GRAPHQL_URL}...")
        session = get_stash_session()

        resp = session.post(
            GRAPHQL_URL,
            json={"query": "{ version { version } }"},
            timeout=5
        )
        resp.raise_for_status()
        v = resp.json().get("data", {}).get("version", {}).get("version", "unknown")
        STASH_VERSION = v
        STASH_CONNECTED = True
        logger.info(f"✅ Connected to Stash! Version: {v}")
        return True
    except Exception as e:
        STASH_CONNECTED = False
        logger.error(f"❌ Failed to connect to Stash: {e}")
        logger.error("Please check STASH_URL and authentication in your config.")
        return False

def pad_image_to_portrait(image_data: bytes, target_width: int = 400, target_height: int = 600) -> Tuple[bytes, str]:
    """
    Pad an image to a portrait 2:3 aspect ratio with a dark background.
    Uses contain+pad strategy: scales to fit within target, then pads the rest.
    Returns (image_bytes, content_type).
    """
    if not PILLOW_AVAILABLE:
        return image_data, "image/jpeg"

    try:
        # Open the image
        img = Image.open(io.BytesIO(image_data))

        # Convert to RGB if necessary (handles PNG transparency, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create a dark background for transparent images
            background = Image.new('RGB', img.size, (20, 20, 20))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        # Calculate scaling to fit within target while preserving aspect ratio
        width, height = img.size

        # Scale to fit within the target dimensions (contain strategy)
        scale_w = target_width / width
        scale_h = target_height / height
        scale = min(scale_w, scale_h)  # Use smaller scale to ensure it fits

        new_width = int(width * scale)
        new_height = int(height * scale)

        # Resize the image
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Create the target canvas with dark background
        canvas = Image.new('RGB', (target_width, target_height), (20, 20, 20))

        # Center the image on the canvas
        x_offset = (target_width - new_width) // 2
        y_offset = (target_height - new_height) // 2
        canvas.paste(img, (x_offset, y_offset))

        # Save to bytes
        output = io.BytesIO()
        canvas.save(output, format='JPEG', quality=85)
        return output.getvalue(), "image/jpeg"

    except Exception as e:
        logger.warning(f"Image padding failed: {e}, returning original")
        return image_data, "image/jpeg"

def stash_query(query: str, variables: Dict[str, Any] = None, retries: int = None) -> Dict[str, Any]:
    """Execute a GraphQL query against Stash with retry logic.

    Args:
        query: The GraphQL query string
        variables: Optional query variables
        retries: Number of retries (defaults to STASH_RETRIES config)

    Returns:
        The JSON response from Stash, or an error dict on failure
    """
    if retries is None:
        retries = STASH_RETRIES

    last_error = None
    for attempt in range(retries + 1):
        try:
            session = get_stash_session()
            resp = session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                timeout=STASH_TIMEOUT
            )
            resp.raise_for_status()
            result = resp.json()

            # Check for GraphQL errors in response
            if "errors" in result and result["errors"]:
                error_msgs = [e.get("message", str(e)) for e in result["errors"]]
                logger.warning(f"GraphQL errors in response: {error_msgs}")
                # Still return the result as it may contain partial data

            return result

        except requests.exceptions.Timeout as e:
            last_error = e
            logger.warning(f"Stash API timeout (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                time.sleep(1 * (attempt + 1))  # Exponential backoff

        except requests.exceptions.ConnectionError as e:
            last_error = e
            logger.warning(f"Stash API connection error (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))  # Longer backoff for connection issues

        except requests.exceptions.HTTPError as e:
            last_error = e
            logger.error(f"Stash API HTTP error: {e}")
            # Don't retry client errors (4xx)
            if hasattr(e, 'response') and e.response is not None and 400 <= e.response.status_code < 500:
                break
            if attempt < retries:
                time.sleep(1 * (attempt + 1))

        except Exception as e:
            last_error = e
            logger.error(f"Stash API Query Error: {e}")
            break  # Don't retry unknown errors

    logger.error(f"Stash API failed after {retries + 1} attempts: {last_error}")
    return {"errors": [str(last_error)], "data": {}}

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
        "Type": "Folder",
        "IsFolder": True,
        "CollectionType": "movies",
        "ChildCount": filter_count,
        "RecursiveItemCount": filter_count,
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},
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
        "Type": "Folder",
        "IsFolder": True,
        "CollectionType": "movies",
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": item_id
        }
    }

# --- Jellyfin Models & Helpers ---
# Note: SERVER_ID is now configured at the top of the file and loaded from config
ACCESS_TOKEN = str(uuid.uuid4())

def make_guid(numeric_id: str) -> str:
    """Convert a numeric ID to a GUID-like format that Jellyfin clients expect."""
    # Pad the ID and format as a pseudo-GUID
    padded = str(numeric_id).zfill(32)
    return f"{padded[:8]}-{padded[8:12]}-{padded[12:16]}-{padded[16:20]}-{padded[20:32]}"

def extract_numeric_id(guid_id: str) -> str:
    """Extract numeric ID from a GUID format, or return as-is if already numeric."""
    if "-" in guid_id:
        # It's a GUID, extract the numeric part
        numeric = guid_id.replace("-", "").lstrip("0")
        return numeric if numeric else "0"
    return guid_id

def format_jellyfin_item(scene: Dict[str, Any], parent_id: str = "root-scenes") -> Dict[str, Any]:
    raw_id = str(scene.get("id"))
    item_id = f"scene-{raw_id}"  # Simple ID format like studios use
    date = scene.get("date")
    files = scene.get("files", [])
    path = files[0].get("path") if files else ""
    duration = files[0].get("duration", 0) if files else 0

    # Title fallback: title -> code -> filename (without extension) -> Scene #
    title = scene.get("title") or scene.get("code")
    if not title and path:
        # Extract filename without extension, like Stash does
        import os
        filename = os.path.basename(path)
        title = os.path.splitext(filename)[0] if filename else None
    if not title:
        title = f"Scene {raw_id}"
    studio = scene.get("studio", {}).get("name") if scene.get("studio") else None
    description = scene.get("details") or ""  # Stash uses 'details' for description
    tags = scene.get("tags", [])
    performers = scene.get("performers", [])

    # Simplified item format - minimal fields for compatibility
    item = {
        "Name": title,
        "SortName": title,
        "Id": item_id,
        "ServerId": SERVER_ID,
        "Type": "Movie",
        "IsFolder": False,
        "MediaType": "Video",
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},  # Triggers image requests
        "BackdropImageTags": [],
        "RunTimeTicks": int(duration * 10000000) if duration else 0,
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": item_id
        }
    }

    # Add optional fields only if they exist
    if date:
        item["ProductionYear"] = int(date[:4])
        item["PremiereDate"] = f"{date}T00:00:00.0000000Z"

    # Build overview from description and/or studio
    overview_parts = []
    if description:
        overview_parts.append(description)
    if studio:
        overview_parts.append(f"Studio: {studio}")
    if overview_parts:
        item["Overview"] = "\n\n".join(overview_parts)

    # Add tags
    if tags:
        item["Tags"] = [t.get("name") for t in tags if t.get("name")]
        item["Genres"] = item["Tags"][:5]  # Infuse may show genres

    # Add performers as "People" (Jellyfin format) with image support
    # Use person- prefix for People to match Jellyfin's expected format
    if performers:
        people_list = []
        for p in performers:
            if p.get("name"):
                person = {
                    "Name": p.get("name"),
                    "Type": "Actor",
                    "Role": "",
                    "Id": f"person-{p.get('id')}",
                    "PrimaryImageTag": "img" if p.get("image_path") else None
                }
                if p.get("image_path"):
                    person["ImageTags"] = {"Primary": "img"}
                people_list.append(person)
        item["People"] = people_list

    if path:
        item["Path"] = path
        item["LocationType"] = "FileSystem"

        # Build MediaStreams for video and subtitles
        media_streams = [
            {
                "Index": 0,
                "Type": "Video",
                "Codec": "h264",
                "IsDefault": True,
                "IsForced": False,
                "IsExternal": False
            }
        ]

        # Add subtitle streams from captions
        captions = scene.get("captions") or []
        for idx, caption in enumerate(captions):
            lang_code = caption.get("language_code", "und")
            caption_type = (caption.get("caption_type", "") or "").lower()

            # Normalize caption_type to srt or vtt (default to vtt if unknown)
            if caption_type not in ("srt", "vtt"):
                caption_type = "vtt"

            # Map caption_type to codec
            codec = "srt" if caption_type == "srt" else "webvtt"

            # Get human-readable language name
            lang_names = {
                "en": "English", "de": "German", "es": "Spanish",
                "fr": "French", "it": "Italian", "nl": "Dutch",
                "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
                "zh": "Chinese", "ru": "Russian", "und": "Unknown"
            }
            display_lang = lang_names.get(lang_code, lang_code.upper())

            media_streams.append({
                "Index": idx + 1,
                "Type": "Subtitle",
                "Codec": codec,
                "Language": lang_code,
                "DisplayLanguage": display_lang,
                "DisplayTitle": f"{display_lang} ({caption_type.upper()})",
                "Title": display_lang,
                "IsDefault": idx == 0,  # First subtitle is default
                "IsForced": False,
                "IsExternal": True,
                "IsTextSubtitleStream": True,
                "SupportsExternalStream": True,
                "DeliveryMethod": "External",
                "DeliveryUrl": f"Subtitles/{idx + 1}/0/Stream.{caption_type}"
            })

        item["HasSubtitles"] = len(captions) > 0
        item["MediaSources"] = [{
            "Id": item_id,
            "Path": path,
            "Protocol": "Http",
            "Type": "Default",
            "Container": "mp4",
            "Name": title,
            "SupportsDirectPlay": True,
            "SupportsDirectStream": True,
            "SupportsTranscoding": False,
            "MediaStreams": media_streams
        }]

    return item

# --- API Endpoints ---

async def endpoint_root(request):
    """Infuse might check root for life."""
    return RedirectResponse(url="/System/Info/Public")

async def endpoint_system_info(request):
    logger.debug("Providing System Info")
    return JSONResponse({
        "ServerName": SERVER_NAME,
        "Version": "10.8.13",
        "Id": SERVER_ID,
        "OperatingSystem": "Linux",
        "SupportsLibraryMonitor": False,
        "WebSocketPortNumber": PROXY_PORT,
        "CompletedInstallations": [{"Guid": SERVER_ID, "Name": SERVER_NAME}],
        "CanSelfRestart": False,
        "CanLaunchWebBrowser": False,
        "LocalAddress": f"http://{PROXY_BIND}:{PROXY_PORT}"
    })

async def endpoint_public_info(request):
    return JSONResponse({
        "LocalAddress": f"http://{PROXY_BIND}:{PROXY_PORT}",
        "ServerName": SERVER_NAME,
        "Version": "10.8.13",
        "Id": SERVER_ID,
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux"
    })

async def endpoint_authenticate_by_name(request):
    try:
        data = await request.json()
    except:
        # Sometimes clients send empty body or form data?
        data = {}

    username = data.get("Username", "User")
    pw = data.get("Pw", "")

    logger.info(f"Auth attempt for user: {username}")
    logger.debug(f"Auth password check: input len={len(pw)}, expected len={len(SJS_PASSWORD)}")

    # Accept config password (strip whitespace from both for comparison)
    if pw.strip() == SJS_PASSWORD.strip():
        # Clear any failed auth attempts for this IP on successful login
        client_ip = get_client_ip(request.scope)
        if client_ip in _ip_failures:
            del _ip_failures[client_ip]
            logger.debug(f"Cleared auth failure tracking for {client_ip} after successful login")

        record_auth_attempt(success=True)
        logger.info(f"Auth SUCCESS for user {SJS_USER}")
        return JSONResponse({
            "User": {
                "Name": username,
                "Id": SJS_USER,
                "Policy": {"IsAdministrator": True}
            },
            "SessionInfo": {
                "UserId": SJS_USER,
                "IsActive": True
            },
            "AccessToken": ACCESS_TOKEN,
            "ServerId": SERVER_ID
        })
    else:
        record_auth_attempt(success=False)
        logger.warning("Auth FAILED - Invalid Key")
        return JSONResponse({"error": "Invalid Token"}, status_code=401)

async def endpoint_users(request):
    return JSONResponse([{
        "Name": "Stash User",
        "Id": SJS_USER,
        "HasPassword": True,
        "Policy": {"IsAdministrator": True, "EnableContentDeletion": False}
    }])

async def endpoint_user_by_id(request):
    # Return user profile
    return JSONResponse({
        "Name": "Stash User",
        "Id": SJS_USER,
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "Policy": {
            "IsAdministrator": True,
            "IsHidden": False,
            "IsDisabled": False,
            "EnableUserPreferenceAccess": True,
            "EnableRemoteAccess": True,
            "EnableContentDeletion": False,
            "EnablePlaybackRemuxing": True,
            "ForceRemoteSourceTranscoding": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": True,
            "EnableVideoPlaybackTranscoding": True
        },
        "Configuration": {
            "PlayDefaultAudioTrack": True,
            "SubtitleLanguagePreference": "",
            "DisplayMissingEpisodes": False,
            "GroupedFolders": [],
            "SubtitleMode": "Default",
            "DisplayCollectionsView": False,
            "EnableLocalPassword": False,
            "OrderedViews": [],
            "LatestItemsExcludes": [],
            "MyMediaExcludes": [],
            "HidePlayedInLatest": True,
            "RememberAudioSelections": True,
            "RememberSubtitleSelections": True,
            "EnableNextEpisodeAutoPlay": True
        }
    })

async def endpoint_user_views(request):
    items = [
        {
            "Name": "Scenes",
            "Id": "root-scenes",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-scenes"}
        },
        {
            "Name": "Studios",
            "Id": "root-studios",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-studios"}
        },
        {
            "Name": "Performers",
            "Id": "root-performers",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-performers"}
        },
        {
            "Name": "Groups",
            "Id": "root-groups",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-groups"}
        }
    ]

    # Add Tags folder if enabled
    if ENABLE_TAG_FILTERS:
        items.append({
            "Name": "Tags",
            "Id": "root-tags",
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-tags"}
        })

    # Add tag group folders (sorted alphabetically)
    for tag_name in sorted(TAG_GROUPS, key=str.lower):
        tag_id = f"tag-{tag_name.lower().replace(' ', '-')}"
        items.append({
            "Name": tag_name,
            "Id": tag_id,
            "ServerId": SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": tag_id}
        })

    return JSONResponse({
        "Items": items,
        "TotalRecordCount": len(items)
    })

async def endpoint_grouping_options(request):
    # Infuse requests this and if it 404s, it shows "an error occurred"
    return JSONResponse([])

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
    # Infuse requests next up episodes - return empty
    return JSONResponse({"Items": [], "TotalRecordCount": 0})

async def endpoint_latest_items(request):
    """Return recently added items for the Infuse home page, personalized by library."""
    # Get parent_id to filter by library
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 16)

    logger.debug(f"Latest items request - ParentId: {parent_id}, Limit: {limit}")

    # Full scene fields for queries
    scene_fields = "id title code date details files { path duration } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

    items = []

    # Check if this library is in LATEST_GROUPS
    def is_in_latest_groups(parent_id):
        if parent_id == "root-scenes":
            return "Scenes" in LATEST_GROUPS
        elif parent_id and parent_id.startswith("tag-"):
            tag_slug = parent_id[4:]
            for t in TAG_GROUPS:
                if t.lower().replace(' ', '-') == tag_slug:
                    return t in LATEST_GROUPS
        return False

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
        # Return latest scenes with this specific tag
        tag_slug = parent_id[4:]  # Remove "tag-" prefix

        # Find the matching tag name from TAG_GROUPS config
        tag_name = None
        for t in TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break

        if tag_name:
            # Find the tag ID
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) {
                    tags { id name }
                }
            }"""
            tag_res = stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])

            # Find exact match
            tag_id = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_id = t["id"]
                    break

            if tag_id:
                # Query scenes with this tag, sorted by created_at
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

    logger.debug(f"Returning {len(items)} latest items for {parent_id}")
    return JSONResponse(items)

async def endpoint_display_preferences(request):
    # Infuse requests display/user preferences
    return JSONResponse({
        "Id": "usersettings",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {},
        "ScrollDirection": "Horizontal",
        "ShowBackdrop": True,
        "RememberSorting": False,
        "ShowSidebar": False
    })

def get_stash_sort_params(request) -> Tuple[str, str]:
    """Map Jellyfin SortBy/SortOrder to Stash sort/direction."""
    # Get sort parameters from request
    sort_by_raw = request.query_params.get("SortBy") or request.query_params.get("sortBy") or "PremiereDate"
    sort_order = request.query_params.get("SortOrder") or request.query_params.get("sortOrder") or "Descending"

    # Infuse sends comma-separated list like "DateCreated,SortName,ProductionYear"
    # Take the first field as the primary sort
    sort_by = sort_by_raw.split(",")[0].strip()

    # Map Jellyfin sort fields to Stash
    # DateCreated = when item was added to library (maps to created_at in Stash)
    # PremiereDate/ProductionYear = release date (maps to date in Stash)
    sort_mapping = {
        "SortName": "title",
        "Name": "title",
        "PremiereDate": "date",
        "DateCreated": "created_at",  # Date added to library
        "DatePlayed": "last_played_at",
        "ProductionYear": "date",
        "Random": "random",
        "Runtime": "duration",
        "CommunityRating": "rating",
        "PlayCount": "play_count",
    }

    stash_sort = sort_mapping.get(sort_by, "date")
    stash_direction = "ASC" if sort_order == "Ascending" else "DESC"

    logger.debug(f"Sort mapping: {sort_by_raw} -> {sort_by} -> {stash_sort} {stash_direction}")

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

    # Debug: Log ALL query params to understand what Infuse is sending
    all_params = dict(request.query_params)
    logger.debug(f"Items endpoint - ALL PARAMS: {all_params}")
    logger.debug(f"Items endpoint - ParentId: {parent_id}, Ids: {ids}, PersonIds: {person_ids}, SearchTerm: {search_term}, StartIndex: {start_index}, Limit: {limit}, Sort: {sort_field} {sort_direction}")

    items = []
    total_count = 0

    # Full scene fields for queries (include performer image_path for People images, captions for subtitles)
    scene_fields = "id title code date details files { path duration } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

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
        # Handle search from Infuse - query Stash with the search term
        # Strip any quotes that Infuse might add around the search term
        clean_search = search_term.strip('"\'')

        logger.info(f"🔍 Search: '{clean_search}'")

        # Get count of matching scenes
        count_q = """query CountScenes($q: String!) {
            findScenes(filter: {q: $q}) { count }
        }"""
        count_res = stash_query(count_q, {"q": clean_search})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        # Query Stash with the search term
        # Note: Stash's q parameter already provides relevance-based filtering
        # We use date DESC as the secondary sort for consistent ordering
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

                # Calculate page
                page = (start_index // limit) + 1

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
                    q = """query FindPerformers($performer_filter: PerformerFilterType, $page: Int!, $per_page: Int!) {
                        findPerformers(
                            performer_filter: $performer_filter,
                            filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}
                        ) {
                            performers { id name image_path scene_count }
                        }
                    }"""
                    res = stash_query(q, {"performer_filter": graphql_filter, "page": page, "per_page": limit})
                    performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])
                    logger.debug(f"Saved filter returned {len(performers)} performers (page {page}, total {total_count})")
                    for p in performers:
                        performer_item = {
                            "Name": p["name"],
                            "Id": f"performer-{p['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "Folder",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": p.get("scene_count", 0),
                            "RecursiveItemCount": p.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"performer-{p['id']}"},
                            "ImageTags": {"Primary": "img"} if p.get("image_path") else {}
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
                    q = """query FindStudios($studio_filter: StudioFilterType, $page: Int!, $per_page: Int!) {
                        findStudios(
                            studio_filter: $studio_filter,
                            filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}
                        ) {
                            studios { id name image_path scene_count }
                        }
                    }"""
                    res = stash_query(q, {"studio_filter": graphql_filter, "page": page, "per_page": limit})
                    studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
                    logger.debug(f"Saved filter returned {len(studios)} studios (page {page}, total {total_count})")
                    for s in studios:
                        studio_item = {
                            "Name": s["name"],
                            "Id": f"studio-{s['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "Folder",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": s.get("scene_count", 0),
                            "RecursiveItemCount": s.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"},
                            "ImageTags": {"Primary": "img"} if s.get("image_path") else {}
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
                    q = """query FindGroups($group_filter: GroupFilterType, $page: Int!, $per_page: Int!) {
                        findGroups(
                            group_filter: $group_filter,
                            filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}
                        ) {
                            groups { id name scene_count }
                        }
                    }"""
                    res = stash_query(q, {"group_filter": graphql_filter, "page": page, "per_page": limit})
                    groups = res.get("data", {}).get("findGroups", {}).get("groups", [])
                    logger.debug(f"Saved filter returned {len(groups)} groups (page {page}, total {total_count})")
                    for g in groups:
                        group_item = {
                            "Name": g["name"],
                            "Id": f"group-{g['id']}",
                            "ServerId": SERVER_ID,
                            "Type": "Folder",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": g.get("scene_count", 0),
                            "RecursiveItemCount": g.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"group-{g['id']}"},
                            "ImageTags": {"Primary": "img"}
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

                    q = """query FindTags($tag_filter: TagFilterType, $page: Int!, $per_page: Int!) {
                        findTags(
                            tag_filter: $tag_filter,
                            filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}
                        ) {
                            count
                            tags { id name scene_count image_path favorite }
                        }
                    }"""
                    res = stash_query(q, {"tag_filter": graphql_filter, "page": stash_page, "per_page": STASH_PAGE_SIZE})
                    data = res.get("data", {}).get("findTags", {})
                    total_count = data.get("count", 0)
                    all_tags = data.get("tags", [])

                    # Slice from offset_in_page, up to limit items
                    tags = all_tags[offset_in_page:offset_in_page + limit]

                    # If we need more items than remaining in this page, fetch next page too
                    while len(tags) < limit and (stash_page * STASH_PAGE_SIZE) < total_count:
                        stash_page += 1
                        res = stash_query(q, {"tag_filter": graphql_filter, "page": stash_page, "per_page": STASH_PAGE_SIZE})
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
                            "Type": "Folder",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": t.get("scene_count", 0),
                            "RecursiveItemCount": t.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": t.get("favorite", False), "Played": False, "Key": f"tagitem-{t['id']}"},
                            "ImageTags": {"Primary": "img"}  # Always set - we serve text icon if no Stash image
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
        # We add it as an extra item (not affecting pagination of actual scenes)
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-scenes"))

        # Total count includes Filters folder if present
        total_count = scene_count + 1 if has_filters else scene_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1

        # Then get paginated scenes with sort from request
        q = f"""query FindScenes($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = stash_query(q, {"page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s, parent_id="root-scenes"))

    elif parent_id == "root-studios":
        # Get total count
        count_q = """query { findStudios { count } }"""
        count_res = stash_query(count_q)
        studio_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

        # Check if there are saved filters for studios (only if ENABLE_FILTERS is on)
        has_filters = False
        if ENABLE_FILTERS:
            saved_filters = stash_get_saved_filters("STUDIOS")
            has_filters = len(saved_filters) > 0

        # On first page, add FILTERS folder at the top if there are saved filters
        # We add it as an extra item (not affecting pagination of actual studios)
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-studios"))

        # Total count includes Filters folder if present
        total_count = studio_count + 1 if has_filters else studio_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1

        q = """query FindStudios($page: Int!, $per_page: Int!) {
            findStudios(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = stash_query(q, {"page": page, "per_page": limit})
        for s in res.get("data", {}).get("findStudios", {}).get("studios", []):
            studio_item = {
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": SERVER_ID,
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": s.get("scene_count", 0),
                "RecursiveItemCount": s.get("scene_count", 0),
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"}
            }
            # Add image if available
            if s.get("image_path"):
                studio_item["ImageTags"] = {"Primary": "img"}
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
        # We add it as an extra item (not affecting pagination of actual performers)
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-performers"))

        # Total count includes Filters folder if present
        total_count = performer_count + 1 if has_filters else performer_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1

        q = """query FindPerformers($page: Int!, $per_page: Int!) {
            findPerformers(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                performers { id name image_path scene_count }
            }
        }"""
        res = stash_query(q, {"page": page, "per_page": limit})
        for p in res.get("data", {}).get("findPerformers", {}).get("performers", []):
            performer_item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": SERVER_ID,
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": p.get("scene_count", 0),
                "RecursiveItemCount": p.get("scene_count", 0),
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"performer-{p['id']}"}
            }
            if p.get("image_path"):
                performer_item["ImageTags"] = {"Primary": "img"}
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
        # Get total count - Stash uses "movies" for groups
        count_q = """query { findMovies { count } }"""
        count_res = stash_query(count_q)
        group_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)

        # Check if there are saved filters for groups (only if ENABLE_FILTERS is on)
        has_filters = False
        if ENABLE_FILTERS:
            saved_filters = stash_get_saved_filters("GROUPS")
            has_filters = len(saved_filters) > 0

        # On first page, add FILTERS folder at the top if there are saved filters
        # We add it as an extra item (not affecting pagination of actual groups)
        prepend_filters = start_index == 0 and has_filters
        if prepend_filters:
            items.append(format_filters_folder("root-groups"))

        # Total count includes Filters folder if present
        total_count = group_count + 1 if has_filters else group_count

        # Use a fixed page size (50) for consistent pagination
        # This avoids misalignment when the last page has a smaller limit
        FIXED_PAGE_SIZE = 50

        # Calculate which Stash page(s) we need and the offset within that page
        stash_page = (start_index // FIXED_PAGE_SIZE) + 1
        offset_within_page = start_index % FIXED_PAGE_SIZE
        items_needed = limit

        logger.debug(f"Groups pagination: startIndex={start_index}, limit={limit}, stash_page={stash_page}, offset_within_page={offset_within_page}")

        # Query for movies - fetch using fixed page size
        q = """query FindMovies($page: Int!, $per_page: Int!) {
            findMovies(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                movies { id name scene_count }
            }
        }"""

        # Fetch pages until we have enough items
        fetched_movies = []
        current_page = stash_page
        while len(fetched_movies) < offset_within_page + items_needed:
            res = stash_query(q, {"page": current_page, "per_page": FIXED_PAGE_SIZE})
            page_movies = res.get("data", {}).get("findMovies", {}).get("movies", [])
            if not page_movies:
                break  # No more data
            fetched_movies.extend(page_movies)
            current_page += 1
            # Safety: don't fetch more than 2 pages
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
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": m.get("scene_count", 0),
                "RecursiveItemCount": m.get("scene_count", 0),
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"group-{m['id']}"},
                # Always advertise image - endpoint will try to fetch and fall back to placeholder if needed
                "ImageTags": {"Primary": "img"}
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
            "Type": "Folder",
            "IsFolder": True,
            "CollectionType": "movies",
            "ParentId": parent_id,
            "ImageTags": {"Primary": "img"},
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
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
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
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
            })
            items_count += 1

        total_count = items_count

    elif parent_id == "tags-favorites":
        # Show favorite tags as browsable folders
        q = """query FindTags($page: Int!, $per_page: Int!) {
            findTags(tag_filter: {favorite: true}, filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                count
                tags { id name scene_count image_path }
            }
        }"""
        page = (start_index // limit) + 1
        res = stash_query(q, {"page": page, "per_page": limit})
        data = res.get("data", {}).get("findTags", {})
        total_count = data.get("count", 0)
        for t in data.get("tags", []):
            tag_item = {
                "Name": t["name"],
                "Id": f"tagitem-{t['id']}",
                "ServerId": SERVER_ID,
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": t.get("scene_count", 0),
                "RecursiveItemCount": t.get("scene_count", 0),
                "ParentId": parent_id,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": True, "Played": False, "Key": f"tagitem-{t['id']}"}
            }
            # Always set ImageTags so Infuse requests an image - we serve text icon if no Stash image
            tag_item["ImageTags"] = {"Primary": "img"}
            items.append(tag_item)

    elif parent_id == "tags-all":
        # Show all tags as browsable folders (with paging)
        q = """query FindTags($page: Int!, $per_page: Int!) {
            findTags(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                count
                tags { id name scene_count image_path favorite }
            }
        }"""
        page = (start_index // limit) + 1
        res = stash_query(q, {"page": page, "per_page": limit})
        data = res.get("data", {}).get("findTags", {})
        total_count = data.get("count", 0)
        for t in data.get("tags", []):
            tag_item = {
                "Name": t["name"],
                "Id": f"tagitem-{t['id']}",
                "ServerId": SERVER_ID,
                "Type": "Folder",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": t.get("scene_count", 0),
                "RecursiveItemCount": t.get("scene_count", 0),
                "ParentId": parent_id,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": t.get("favorite", False), "Played": False, "Key": f"tagitem-{t['id']}"}
            }
            # Always set ImageTags so Infuse requests an image - we serve text icon if no Stash image
            tag_item["ImageTags"] = {"Primary": "img"}
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

    # Log pagination info for debugging
    logger.debug(f"Items response: returning {len(items)} items, TotalRecordCount={total_count}, StartIndex={start_index}")
    if len(items) > 0 and total_count > start_index + len(items):
        logger.debug(f"More items available: next page would start at {start_index + len(items)}")

    return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})

async def endpoint_item_details(request):
    item_id = request.path_params.get("item_id")

    # Full scene fields for queries (include performer image_path for People images, captions for subtitles)
    scene_fields = "id title code date details files { path duration } studio { name } tags { name } performers { name id image_path } captions { language_code caption_type }"

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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
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
                    "Type": "Folder",
                    "CollectionType": "movies",
                    "IsFolder": True,
                    "ImageTags": {"Primary": "img"},
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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"} if has_image else {},
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
        q = """query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path scene_count } }"""
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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"} if has_image else {},
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
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
        q = """query FindMovie($id: ID!) { findMovie(id: $id) { id name front_image_path scene_count } }"""
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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"} if has_image else {},
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
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
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"} if has_image else {},
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

    # Log when video stops at INFO level
    if "/Stopped" in path:
        try:
            body = await request.json()
            item_id = body.get("ItemId", "unknown")
            # Get title from active streams cache, or fetch it
            if item_id in _active_streams:
                title = _active_streams[item_id]["title"]
                mark_stream_stopped(item_id, from_stop_notification=True)
                logger.info(f"⏹ Stream stopped: {title} ({item_id})")
            elif item_id.startswith("scene-"):
                # Stream wasn't tracked (e.g., after server restart) - still mark as recently stopped
                title = get_scene_title(item_id)
                mark_stream_stopped(item_id, from_stop_notification=True)
                logger.info(f"⏹ Stream stopped: {title} ({item_id})")
            else:
                logger.info(f"⏹ Stream stopped: {item_id}")
        except:
            logger.info("⏹ Stream stopped")

    return JSONResponse({})

async def endpoint_playback_info(request):
    """Return playback info with subtitle streams for a scene."""
    item_id = request.path_params.get("item_id")

    if not item_id or not item_id.startswith("scene-"):
        # Generic fallback
        return JSONResponse({
            "MediaSources": [{
                "Id": "src1",
                "Protocol": "Http",
                "MediaStreams": [],
                "SupportsDirectPlay": True,
                "SupportsTranscoding": False
            }],
            "PlaySessionId": "session-1"
        })

    numeric_id = item_id.replace("scene-", "")

    # Query scene to get captions
    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            id
            title
            files { path duration }
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
                "Protocol": "Http",
                "MediaStreams": [],
                "SupportsDirectPlay": True,
                "SupportsTranscoding": False
            }],
            "PlaySessionId": "session-1"
        })

    scene = scene_data
    files = scene.get("files", [])
    path = files[0].get("path", "") if files else ""
    captions = scene.get("captions") or []

    # Build MediaStreams
    media_streams = [
        {
            "Index": 0,
            "Type": "Video",
            "Codec": "h264",
            "IsDefault": True,
            "IsForced": False,
            "IsExternal": False
        }
    ]

    # Add subtitle streams
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
            "Index": idx + 1,
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

    return JSONResponse({
        "MediaSources": [{
            "Id": item_id,
            "Path": path,
            "Protocol": "Http",
            "Type": "Default",
            "Container": "mp4",
            "SupportsDirectPlay": True,
            "SupportsDirectStream": True,
            "SupportsTranscoding": False,
            "MediaStreams": media_streams
        }],
        "PlaySessionId": f"session-{item_id}"
    })

def get_numeric_id(item_id: str) -> str:
    """Extract numeric ID from various formats: scene-123, studio-456, or GUID."""
    if item_id.startswith("scene-"):
        return item_id.replace("scene-", "")
    elif item_id.startswith("studio-"):
        return item_id.replace("studio-", "")
    elif "-" in item_id:
        # GUID format - extract numeric part
        return extract_numeric_id(item_id)
    return item_id

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

async def endpoint_subtitle(request):
    """Proxy subtitle/caption file from Stash."""
    item_id = request.path_params.get("item_id")
    subtitle_index = int(request.path_params.get("subtitle_index", 1))

    # Get the scene's numeric ID
    numeric_id = get_numeric_id(item_id)

    # Query Stash for captions to get the correct filename
    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
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

        # Get the caption by index (1-based from Jellyfin)
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

def generate_text_icon(text: str, width: int = 400, height: int = 600,
                       max_chars_per_line: int = 16, max_lines: int = 4) -> Tuple[bytes, str]:
    """Generate a portrait 2:3 PNG icon with word-wrapped text label.

    Args:
        text: The text to display
        width: Image width in pixels
        height: Image height in pixels
        max_chars_per_line: Maximum characters per line before wrapping
        max_lines: Maximum number of lines (text truncated after this)
    """
    if not PILLOW_AVAILABLE:
        logger.debug("Pillow not available, returning placeholder PNG")
        return PLACEHOLDER_PNG, "image/png"

    try:
        from PIL import ImageDraw, ImageFont
        import os

        # Create portrait image with dark background
        img = Image.new('RGB', (width, height), (26, 26, 46))
        draw = ImageDraw.Draw(img)

        # Text color (Stash-like blue)
        text_color = (74, 144, 217)  # #4a90d9

        # Maximum text area (leave padding on sides)
        PADDING = 30
        max_text_width = width - (PADDING * 2)

        # Try to load a font
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]

        font_path_found = None
        for font_path in font_paths:
            if os.path.exists(font_path):
                font_path_found = font_path
                break

        # Word wrap the text first (using character count as rough guide)
        words = text.split()
        lines = []
        current_line = ""

        for word in words:
            test_line = (current_line + " " + word).strip() if current_line else word

            if len(test_line) <= max_chars_per_line:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                if len(word) > max_chars_per_line:
                    current_line = word[:max_chars_per_line - 3] + "..."
                else:
                    current_line = word

        if current_line:
            lines.append(current_line)

        # Truncate to max lines
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            if len(lines[-1]) > max_chars_per_line - 3:
                lines[-1] = lines[-1][:max_chars_per_line - 3] + "..."
            else:
                lines[-1] = lines[-1] + "..."

        # Now find the right font size that fits all lines within max_text_width
        # Start at 48px and scale down if needed
        font_size = 48
        min_font_size = 24
        font = None

        while font_size >= min_font_size:
            if font_path_found:
                try:
                    font = ImageFont.truetype(font_path_found, font_size)
                except (IOError, OSError):
                    font = ImageFont.load_default()
                    break
            else:
                font = ImageFont.load_default()
                break

            # Check if all lines fit
            all_fit = True
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                if line_width > max_text_width:
                    all_fit = False
                    break

            if all_fit:
                break

            font_size -= 2  # Try smaller font

        if font is None:
            font = ImageFont.load_default()

        logger.debug(f"Icon '{text}': {len(lines)} lines, font size {font_size}px")

        # Calculate line dimensions with final font
        line_heights = []
        line_widths = []
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_widths.append(bbox[2] - bbox[0])
            line_heights.append(bbox[3] - bbox[1])

        line_spacing = 10
        total_height = sum(line_heights) + (len(lines) - 1) * line_spacing if lines else 0

        # Center vertically
        start_y = (height - total_height) // 2

        # Draw each line centered horizontally
        current_y = start_y
        for i, line in enumerate(lines):
            x = (width - line_widths[i]) // 2
            draw.text((x, current_y), line, fill=text_color, font=font)
            current_y += line_heights[i] + line_spacing

        # Save as PNG
        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue(), "image/png"

    except Exception as e:
        logger.warning(f"Text icon generation failed: {e}")
        return PLACEHOLDER_PNG, "image/png"

def generate_menu_icon(icon_type: str, width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Generate a menu icon for top-level folders (12 chars wide, 4 lines max)."""
    icon_names = {
        "root-scenes": "Scenes",
        "root-studios": "Studios",
        "root-performers": "Performers",
        "root-groups": "Groups",
        "root-tag": "Tags",
    }

    text = icon_names.get(icon_type, icon_type.replace("root-", "").replace("-", " ").title())
    return generate_text_icon(text, width, height, max_chars_per_line=12, max_lines=4)

def generate_filter_icon(text: str, width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Generate a filter icon (10 chars wide, 6 lines max for poster-sized display)."""
    return generate_text_icon(text, width, height, max_chars_per_line=10, max_lines=6)

def generate_placeholder_icon(item_type: str = "group", width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Generate a placeholder icon for items without images."""
    if not PILLOW_AVAILABLE:
        # Return dark PNG placeholder
        return PLACEHOLDER_PNG, "image/png"

    try:
        from PIL import ImageDraw

        # Create image with dark background
        img = Image.new('RGB', (width, height), (30, 30, 35))
        draw = ImageDraw.Draw(img)

        # Gray placeholder color
        placeholder_color = (80, 80, 90)

        if item_type == "group":
            # Film strip / movie icon
            draw.rectangle([120, 200, 280, 360], outline=placeholder_color, width=6)
            # Film holes on sides
            for y in [220, 270, 320]:
                draw.rectangle([130, y, 150, y+20], fill=placeholder_color)
                draw.rectangle([250, y, 270, y+20], fill=placeholder_color)
        else:
            # Generic placeholder - question mark or film icon
            draw.ellipse([140, 200, 260, 320], outline=placeholder_color, width=6)
            draw.text((180, 230), "?", fill=placeholder_color)

        # Save as PNG
        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue(), "image/png"

    except Exception as e:
        logger.warning(f"Placeholder icon generation failed: {e}")
        return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82', "image/png"

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

    try:
        data, content_type, _ = fetch_from_stash(stash_img_url, extra_headers=image_headers, timeout=30)

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
        logger.error(f"Image proxy error: {e}")
        from starlette.responses import Response

        # For groups, return a placeholder icon instead of transparent pixel
        if item_id.startswith("group-"):
            img_data, content_type = generate_placeholder_icon("group")
            logger.debug(f"Serving placeholder icon for failed group image: {item_id}")
            return Response(content=img_data, media_type=content_type, headers=cache_headers)

        # Return transparent 1x1 PNG as fallback for other types
        return Response(content=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82', media_type='image/png', headers=cache_headers)

async def endpoint_user_items_resume(request):
    """Return resume/in-progress items - currently returns empty."""
    # TODO: Could integrate with Stash's continue_watching or playback history
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_ping(request):
    """Simple ping endpoint for connectivity checks."""
    return Response(content="Stash-Jellyfin Proxy", media_type="text/plain")

async def endpoint_sessions_capabilities(request):
    """Return session capabilities - stub for client compatibility."""
    return JSONResponse({})

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
    """Handle favorite items - returns empty since Stash doesn't sync favorites."""
    # Stash has an 'organized' field but not a favorites system
    # Could potentially use tags to implement favorites in the future
    user_id = request.path_params.get("user_id")
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_user_item_favorite(request):
    """Toggle favorite status on an item - stub that accepts but doesn't persist."""
    # Accept the request but don't actually do anything since Stash doesn't have favorites
    # Could potentially add/remove a "Favorites" tag in Stash in the future
    return JSONResponse({"IsFavorite": True})

async def endpoint_user_item_unfavorite(request):
    """Remove favorite status - stub."""
    return JSONResponse({"IsFavorite": False})

async def endpoint_user_item_rating(request):
    """Update item rating - stub that accepts but doesn't persist."""
    # Stash has a rating100 field (0-100), Jellyfin uses different scales
    # Could potentially sync this in the future
    return JSONResponse({})

async def endpoint_user_played_items(request):
    """Mark item as played - stub."""
    return JSONResponse({})

async def endpoint_user_unplayed_items(request):
    """Mark item as unplayed - stub."""
    return JSONResponse({})

async def endpoint_collections(request):
    """Return collections - maps to Stash groups/movies."""
    # Could return groups as collections, but for now return empty
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_playlists(request):
    """Return playlists - Stash doesn't have playlists."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_genres(request):
    """Return genres - could map to Stash tags."""
    user_id = request.path_params.get("user_id")
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")

    # Return Stash tags as genres
    try:
        q = """query { findTags(filter: {per_page: 100, sort: "name", direction: ASC}) {
            tags { id name scene_count }
        }}"""
        res = stash_query(q)
        tags = res.get("data", {}).get("findTags", {}).get("tags", [])
        items = []
        for t in tags:
            if t.get("scene_count", 0) > 0:
                items.append({
                    "Name": t["name"],
                    "Id": f"genre-{t['id']}",
                    "ServerId": SERVER_ID,
                    "Type": "Genre"
                })
        return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error getting genres: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_persons(request):
    """Return persons - maps to Stash performers."""
    # This is an alternative endpoint for accessing performers
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, MAX_PAGE_SIZE))  # Enforce min=1, max=MAX_PAGE_SIZE

    # Check for searchTerm parameter (Infuse search functionality)
    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm")

    try:
        page = (start_index // limit) + 1

        if search_term:
            # Search for performers matching the search term
            clean_search = search_term.strip('"\'')
            logger.debug(f"Persons search: '{clean_search}'")

            # Count matching performers
            count_q = """query CountPerformers($q: String!) {
                findPerformers(filter: {q: $q}) { count }
            }"""
            count_res = stash_query(count_q, {"q": clean_search})
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

            # Query performers with search term
            q = """query FindPerformers($q: String!, $page: Int!, $per_page: Int!) {
                findPerformers(filter: {q: $q, page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = stash_query(q, {"q": clean_search, "page": page, "per_page": limit})
            logger.debug(f"Persons search '{clean_search}' returned {total_count} matches")
        else:
            # No search term - return all performers
            count_q = """query { findPerformers { count } }"""
            count_res = stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

            q = """query FindPerformers($page: Int!, $per_page: Int!) {
                findPerformers(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = stash_query(q, {"page": page, "per_page": limit})

        performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])

        items = []
        for p in performers:
            has_image = bool(p.get("image_path"))
            item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": SERVER_ID,
                "Type": "Person",
                "ImageTags": {"Primary": "img"} if has_image else {},
                "BackdropImageTags": []
            }
            items.append(item)
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting persons: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_studios(request):
    """Return studios list via /Studios endpoint."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, MAX_PAGE_SIZE))  # Enforce min=1, max=MAX_PAGE_SIZE

    try:
        count_q = """query { findStudios { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

        page = (start_index // limit) + 1
        q = """query FindStudios($page: Int!, $per_page: Int!) {
            findStudios(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = stash_query(q, {"page": page, "per_page": limit})
        studios = res.get("data", {}).get("findStudios", {}).get("studios", [])

        items = []
        for s in studios:
            has_image = bool(s.get("image_path"))
            item = {
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": SERVER_ID,
                "Type": "Studio",
                "ImageTags": {"Primary": "img"} if has_image else {},
                "BackdropImageTags": []
            }
            items.append(item)
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting studios: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_artists(request):
    """Return artists - maps to Stash performers (alternative endpoint)."""
    return await endpoint_persons(request)

async def endpoint_years(request):
    """Return available years for filtering."""
    # Could query Stash for distinct years from scenes
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_similar(request):
    """Return similar items - stub."""
    item_id = request.path_params.get("item_id")
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_recommendations(request):
    """Return recommendations - stub."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_instant_mix(request):
    """Return instant mix playlist - stub."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_intros(request):
    """Return intro/trailer items - stub."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_special_features(request):
    """Return special features - stub."""
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_branding(request):
    """Return branding configuration."""
    return JSONResponse({
        "LoginDisclaimer": None,
        "CustomCss": None,
        "SplashscreenEnabled": False
    })

async def endpoint_media_segments(request):
    """
    Return media segments for a scene - stub endpoint.

    Note: Infuse does not currently support Jellyfin's MediaSegments API
    (intro/outro/chapter skipping). It only uses traditional chapter markers
    embedded in video files. This stub prevents "UNHANDLED ENDPOINT" warnings.
    """
    return JSONResponse({"Items": []})

async def catch_all(request):
    """Catch any unhandled routes and log them for debugging."""
    logger.warning(f"UNHANDLED ENDPOINT: {request.method} {request.url.path} - Query: {dict(request.query_params)}")
    # Return empty success to prevent errors
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

# --- App Construction ---
routes = [
    Route("/", endpoint_root),
    Route("/System/Info", endpoint_system_info),
    Route("/System/Info/Public", endpoint_public_info),
    Route("/System/Ping", endpoint_ping),
    Route("/Branding/Configuration", endpoint_branding),
    Route("/Users/AuthenticateByName", endpoint_authenticate_by_name, methods=["POST"]),
    Route("/Users/{user_id}", endpoint_user_by_id),
    Route("/Users/{user_id}/Views", endpoint_user_views),
    Route("/Users/{user_id}/Items/Latest", endpoint_latest_items),
    Route("/Users/{user_id}/Items/Resume", endpoint_user_items_resume),
    Route("/Users/{user_id}/GroupingOptions", endpoint_grouping_options),
    Route("/Users/{user_id}/FavoriteItems", endpoint_user_favorites),
    Route("/Users/{user_id}/Items/{item_id}/Rating", endpoint_user_item_rating, methods=["POST", "DELETE"]),
    Route("/Users/{user_id}/FavoriteItems/{item_id}", endpoint_user_item_favorite, methods=["POST"]),
    Route("/Users/{user_id}/FavoriteItems/{item_id}/Delete", endpoint_user_item_unfavorite, methods=["POST", "DELETE"]),
    Route("/Users/{user_id}/PlayedItems/{item_id}", endpoint_user_played_items, methods=["POST"]),
    Route("/Users/{user_id}/PlayingItems/{item_id}", endpoint_user_played_items, methods=["POST", "DELETE"]),
    Route("/Users/{user_id}/UnplayedItems/{item_id}", endpoint_user_unplayed_items, methods=["POST", "DELETE"]),
    Route("/Library/VirtualFolders", endpoint_virtual_folders),
    Route("/DisplayPreferences/{prefs_id}", endpoint_display_preferences),
    Route("/Shows/NextUp", endpoint_shows_nextup),
    Route("/Users/{user_id}/Items", endpoint_items),
    Route("/Users/{user_id}/Items/{item_id}", endpoint_item_details),
    Route("/Items", endpoint_items),
    Route("/Items/Counts", endpoint_items_counts),
    Route("/Items/{item_id}/PlaybackInfo", endpoint_playback_info, methods=["GET", "POST"]),
    Route("/Items/{item_id}/Similar", endpoint_similar),
    Route("/Items/{item_id}/Intros", endpoint_intros),
    Route("/Items/{item_id}/SpecialFeatures", endpoint_special_features),
    Route("/Videos/{item_id}/stream", endpoint_stream),
    Route("/Videos/{item_id}/stream.mp4", endpoint_stream),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/Stream.vtt", endpoint_subtitle),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/0/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/Subtitles/{subtitle_index}/0/Stream.vtt", endpoint_subtitle),
    Route("/Videos/{item_id}/{item_id2}/Subtitles/{subtitle_index}/0/Stream.srt", endpoint_subtitle),
    Route("/Videos/{item_id}/{item_id2}/Subtitles/{subtitle_index}/0/Stream.vtt", endpoint_subtitle),
    Route("/Items/{item_id}/Images/Primary", endpoint_image),
    Route("/Items/{item_id}/Images/Thumb", endpoint_image),
    Route("/PlaybackInfo", endpoint_playback_info, methods=["POST", "GET"]),
    Route("/Sessions/Playing", endpoint_sessions, methods=["POST"]),
    Route("/Sessions/Playing/Progress", endpoint_sessions, methods=["POST"]),
    Route("/Sessions/Playing/Stopped", endpoint_sessions, methods=["POST"]),
    Route("/Sessions/Capabilities", endpoint_sessions_capabilities, methods=["POST"]),
    Route("/Sessions/Capabilities/Full", endpoint_sessions_capabilities, methods=["POST"]),
    Route("/Collections", endpoint_collections),
    Route("/Playlists", endpoint_playlists),
    Route("/Genres", endpoint_genres),
    Route("/MusicGenres", endpoint_genres),
    Route("/Persons", endpoint_persons),
    Route("/Studios", endpoint_studios),
    Route("/Artists", endpoint_artists),
    Route("/Years", endpoint_years),
    Route("/Movies/Recommendations", endpoint_recommendations),
    Route("/Items/{item_id}/InstantMix", endpoint_instant_mix),
    Route("/MediaSegments/{item_id}", endpoint_media_segments),
    Route("/{path:path}", catch_all),
]

middleware = [
    Middleware(AuthenticationMiddleware),  # Token validation first
    Middleware(RequestLoggingMiddleware),
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
]

app = Starlette(debug=True, routes=routes, middleware=middleware)

# --- Web UI Server ---
PROXY_RUNNING = False  # Track if proxy is running
PROXY_START_TIME = None  # Track when proxy started

async def ui_index(request):
    """Serve the Web UI."""
    html = WEB_UI_HTML.replace("{{SERVER_NAME}}", SERVER_NAME)
    return Response(html, media_type="text/html")

async def ui_api_status(request):
    """Return proxy status."""
    uptime_seconds = int(time.time() - PROXY_START_TIME) if PROXY_START_TIME else 0
    return JSONResponse({
        "running": PROXY_RUNNING,
        "version": "v5.00",
        "proxyBind": PROXY_BIND,
        "proxyPort": PROXY_PORT,
        "uptime": uptime_seconds,
        "stashConnected": STASH_CONNECTED,
        "stashVersion": STASH_VERSION,
        "stashUrl": STASH_URL
    })

async def ui_api_config(request):
    """Get or set configuration."""
    # Declare globals at top of function (required before any reference)
    global TAG_GROUPS, LATEST_GROUPS, SERVER_NAME, STASH_TIMEOUT, STASH_RETRIES
    global STASH_GRAPHQL_PATH, STASH_VERIFY_TLS, ENABLE_FILTERS, ENABLE_IMAGE_RESIZE
    global ENABLE_TAG_FILTERS, ENABLE_ALL_TAGS
    global IMAGE_CACHE_MAX_SIZE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, REQUIRE_AUTH_FOR_CONFIG
    global LOG_LEVEL, _config_defined_keys, BANNED_IPS, BAN_THRESHOLD, BAN_WINDOW_MINUTES

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
                "LATEST_GROUPS": LATEST_GROUPS,
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
                "TAG_GROUPS", "LATEST_GROUPS", "STASH_TIMEOUT", "STASH_RETRIES",
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
                "LATEST_GROUPS": ", ".join(LATEST_GROUPS) if LATEST_GROUPS else "",
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
                "LATEST_GROUPS": "Scenes",
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
                elif key == "LATEST_GROUPS":
                    LATEST_GROUPS = [t.strip() for t in new_val.split(",") if t.strip()]
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
                elif key == "LATEST_GROUPS":
                    LATEST_GROUPS = ["Scenes"]
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

async def ui_api_logs(request):
    """Return log entries."""
    limit = int(request.query_params.get("limit", 100))
    entries = []

    log_path = os.path.join(LOG_DIR, LOG_FILE) if LOG_DIR else LOG_FILE
    if os.path.isfile(log_path):
        try:
            with open(log_path, 'r') as f:
                lines = f.readlines()
                for line in lines[-limit:]:
                    line = line.strip()
                    if not line:
                        continue
                    # Parse log format: 2025-12-03 12:08:28,115 - stash-jellyfin-proxy - INFO - message
                    parts = line.split(" - ", 3)
                    if len(parts) >= 4:
                        entries.append({
                            "timestamp": parts[0],
                            "level": parts[2],
                            "message": parts[3]
                        })
                    else:
                        entries.append({
                            "timestamp": "",
                            "level": "INFO",
                            "message": line
                        })
        except Exception as e:
            pass

    return JSONResponse({
        "entries": entries,
        "logPath": log_path
    })

async def ui_api_streams(request):
    """Return active streams."""
    streams = []
    now = time.time()
    for scene_id, info in _active_streams.items():
        # Only include streams active in last 5 minutes
        if now - info.get("last_seen", 0) < 300:
            streams.append({
                "id": scene_id,
                "title": info.get("title", scene_id),
                "performer": info.get("performer", ""),
                "started": info.get("started", 0),
                "lastSeen": info.get("last_seen", 0),
                "user": info.get("user", SJS_USER),
                "clientIp": info.get("client_ip", "unknown"),
                "clientType": info.get("client_type", "unknown")
            })
    return JSONResponse({"streams": streams})

async def ui_api_stats(request):
    """Return Stash library stats and proxy usage stats."""
    # Get Stash library stats
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
            "groups": stats_data.get("movie_count", 0)
        }
    except Exception as e:
        logger.debug(f"Could not fetch Stash stats: {e}")

    # Get proxy stats
    proxy_stats = get_proxy_stats()

    return JSONResponse({
        "stash": stash_stats,
        "proxy": proxy_stats
    })

async def ui_api_stats_reset(request):
    """Reset all proxy statistics."""
    global _proxy_stats, _stats_dirty
    
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)
    
    logger.info("Statistics reset requested via Web UI")
    
    # Reset all stats to initial values
    _proxy_stats = {
        "total_streams": 0,
        "streams_today": 0,
        "streams_today_date": time.strftime("%Y-%m-%d"),
        "unique_ips_today": [],
        "auth_success": 0,
        "auth_failed": 0,
        "play_counts": {},
    }
    _stats_dirty = True
    save_proxy_stats()
    
    return JSONResponse({"success": True, "message": "Statistics reset"})

# Global reference for restart functionality
_shutdown_event = None
_restart_requested = False

async def ui_api_restart(request):
    """Restart the proxy server."""
    global _restart_requested

    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    logger.info("Restart requested via Web UI")
    _restart_requested = True

    # Schedule the shutdown after responding (restart happens after main loop exits)
    async def delayed_shutdown():
        await asyncio.sleep(1)  # Allow response to be sent
        logger.info("Shutting down for restart...")
        if _shutdown_event:
            _shutdown_event.set()

    asyncio.create_task(delayed_shutdown())
    return JSONResponse({"success": True, "message": "Restarting..."})

async def ui_api_auth_config(request):
    """Authenticate for config access."""
    if request.method != "POST":
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    try:
        data = await request.json()
        password = data.get("password", "")

        # Debug: log password lengths for troubleshooting
        logger.debug(f"Auth attempt: input len={len(password)}, expected len={len(SJS_PASSWORD)}")

        # Strip any whitespace from both passwords for comparison
        input_pw = password.strip()
        expected_pw = SJS_PASSWORD.strip()

        if input_pw == expected_pw:
            logger.info("Config authentication successful")
            return JSONResponse({"success": True})
        else:
            logger.warning(f"Config authentication failed - password mismatch (input: {len(input_pw)} chars, expected: {len(expected_pw)} chars)")
            return JSONResponse({"success": False, "error": "Invalid password"})
    except Exception as e:
        logger.error(f"Config authentication error: {e}")
        return JSONResponse({"success": False, "error": str(e)})

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
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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
    parser = argparse.ArgumentParser(description="Stash-Jellyfin Proxy Server")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (overrides config)")
    parser.add_argument("--no-log-file", action="store_true", help="Disable file logging")
    parser.add_argument("--no-ui", action="store_true", help="Disable Web UI server")
    args = parser.parse_args()

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

    logger.info(f"--- Stash-Jellyfin Proxy v5.00 ---")

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
        # Set up signal handlers
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
