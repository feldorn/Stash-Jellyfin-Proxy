"""Shared runtime state — the single source of truth for config and
per-process state that extracted modules need to read.

**Usage pattern:**

    from stash_jellyfin_proxy import runtime
    resp = requests.get(runtime.STASH_URL + "/something")

Never `from stash_jellyfin_proxy.runtime import STASH_URL` — that captures the value at
import time. Always reach attributes through the module object so reads
see current values (config can change via the Web UI hot-reload).

**Ownership during the Phase 0.6 refactor window:**

The monolith `stash_jellyfin_proxy.py` reads the on-disk config and writes
every derived value into this module. Extracted modules read only — they
never write runtime state themselves (that'd undercut the single-source
rule). When extraction completes and the monolith is retired, init will
move into `stash_jellyfin_proxy.app.bootstrap()` or equivalent and this file becomes the
primary store.

Until then the monolith keeps its own module-level copies too (dual-write
during transition) so code still in the monolith keeps working unchanged.
"""
from typing import Any, Dict, List, Optional, Set

# --- Stash connection ---
STASH_URL: str = "https://stash:9999"
STASH_API_KEY: str = ""
STASH_GRAPHQL_PATH: str = "/graphql"
STASH_VERIFY_TLS: bool = False
STASH_TIMEOUT: int = 30
STASH_RETRIES: int = 3
GRAPHQL_URL: str = ""  # derived: STASH_URL + STASH_GRAPHQL_PATH
STASH_SESSION: Any = None
STASH_VERSION: str = ""
STASH_CONNECTED: bool = False

# --- Proxy identity + bind ---
PROXY_BIND: str = "0.0.0.0"
PROXY_PORT: int = 8096
UI_PORT: int = 8097
SERVER_NAME: str = "Stash Media Server"
SERVER_ID: str = ""

# --- Client auth ---
SJS_USER: str = ""
SJS_PASSWORD: str = ""
ACCESS_TOKEN: str = ""

# --- Libraries / content ---
TAG_GROUPS: List[str] = []
FAVORITE_TAG: str = ""
LATEST_GROUPS: List[str] = []
BANNER_MODE: str = "recent"
BANNER_POOL_SIZE: int = 200
BANNER_TAGS: List[str] = []

# --- Feature toggles ---
ENABLE_FILTERS: bool = True
ENABLE_IMAGE_RESIZE: bool = True
ENABLE_TAG_FILTERS: bool = False
ENABLE_ALL_TAGS: bool = False
REQUIRE_AUTH_FOR_CONFIG: bool = False

# --- Pagination / image cache sizing ---
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200
IMAGE_CACHE_MAX_SIZE: int = 100

# --- Logging ---
LOG_DIR: str = "."
LOG_FILE: str = "stash_jellyfin_proxy.log"
LOG_LEVEL: str = "INFO"
LOG_MAX_SIZE_MB: int = 10
LOG_BACKUP_COUNT: int = 3

# --- IP ban state ---
BANNED_IPS: Set[str] = set()
BAN_THRESHOLD: int = 10
BAN_WINDOW_MINUTES: int = 15

# --- Runtime flags ---
PROXY_RUNNING: bool = False
PROXY_START_TIME: Optional[float] = None

# --- Config file paths ---
CONFIG_FILE: str = ""
LOCAL_CONFIG_FILE: str = ""

# --- Fixed identity strings ---
# The Jellyfin protocol version we pretend to be. Bump when clients
# require newer API features.
JELLYFIN_VERSION: str = "10.11.0"

# Stable per-user UUID derived from SERVER_ID + SJS_USER; computed once
# at bootstrap and published here.
USER_ID: str = ""

# --- Loaded config data ---
# Flat KEY → value dict from the config file (after v2 migration + local
# override merge). Sections dict holds INI-style [section.name] blocks.
config: Dict[str, str] = {}
config_defined_keys: Set[str] = set()
config_sections: Dict[str, Dict[str, str]] = {}

# --- Migration state (surfaced in the Web UI banner) ---
MIGRATION_PERFORMED: bool = False
MIGRATION_LOG: List[str] = []

# --- Image byte cache (Pillow output keyed by (item_id, size)) ---
IMAGE_CACHE: Dict[Any, Any] = {}

# --- Live config state (mutable at runtime via ui_api_config) ---
# Keys that are currently overridden via environment variables — the Web UI
# shows them as read-only. Populated at bootstrap.
env_overrides: List[str] = []
# The Stash tag id for FAVORITE_TAG, cached after first lookup.
favorite_tag_id_cache: Any = None

# --- Series / player profiles (Phase 2) ---
SERIES_TAG: str = "Series"
SERIES_EPISODE_PATTERNS: str = ""
PLAYER_PROFILES: List[Any] = []


def publish(**kwargs):
    """Bulk-set attributes. Used by the monolith bootstrap to copy its
    module-level config values into this shared namespace in one pass."""
    for k, v in kwargs.items():
        globals()[k] = v
