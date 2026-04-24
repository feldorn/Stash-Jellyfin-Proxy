"""Config bootstrap — load, migrate, merge, apply env overrides, publish.

`run_bootstrap(config_file, local_config_file)` is called once at startup
from `stash_jellyfin_proxy.py`. It:

  1. Loads the base config file
  2. Runs v1→v2 migration (in-place on disk if needed)
  3. Merges the local override file on top
  4. Assigns all config keys to their typed Python values
  5. Applies environment-variable overrides (always win over file)
  6. Prints the effective config summary to stdout
  7. Auto-generates SERVER_ID / ACCESS_TOKEN when missing
  8. Publishes all derived values to `stash_jellyfin_proxy.runtime`

After this call, `stash_jellyfin_proxy.runtime` is the single authoritative source of
truth for all config values. The callee can optionally read specific
values from `stash_jellyfin_proxy.runtime` if it needs them locally.
"""
import os
import uuid

import stash_jellyfin_proxy.runtime as runtime
from stash_jellyfin_proxy.config.helpers import (
    parse_bool,
    normalize_path,
    normalize_server_id,
    generate_server_id,
    save_config_value,
    save_server_id_to_config,
)
from stash_jellyfin_proxy.config.loader import load_config
from stash_jellyfin_proxy.config.migration import run_config_migration, CURRENT_CONFIG_VERSION


# ---- Docker-injected ENV defaults (set in Dockerfile ENV block) ----
# Only flag a value as "env override" if it differs from the Docker
# default — Dockerfile bakes these in and every container would look
# like it had user overrides otherwise.
_DOCKER_ENV_DEFAULTS = {
    "PROXY_BIND": "0.0.0.0",
    "PROXY_PORT": "8096",
    "UI_PORT": "8097",
    "LOG_DIR": "/config",
}


def _default_local_config_path(base_path: str) -> str:
    """Derive a sibling .local override path from the base config path.
    e.g. stash_jellyfin_proxy.conf → stash_jellyfin_proxy.local.conf"""
    root, ext = os.path.splitext(base_path)
    return f"{root}.local{ext}" if ext else f"{base_path}.local"


def run_bootstrap(config_file: str, local_config_file: str) -> None:
    """Load config, apply overrides, publish to stash_jellyfin_proxy.runtime.

    All side effects (print, file writes for SERVER_ID/ACCESS_TOKEN)
    happen here. After returning, stash_jellyfin_proxy.runtime holds every config value.
    """
    # ---- Defaults ----
    STASH_URL = "https://stash:9999"
    STASH_API_KEY = ""
    PROXY_BIND = "0.0.0.0"
    PROXY_PORT = 8096
    UI_PORT = 8097
    SJS_USER = ""
    SJS_PASSWORD = ""
    TAG_GROUPS = []
    FAVORITE_TAG = ""
    LATEST_GROUPS = []
    BANNER_MODE = "recent"
    BANNER_POOL_SIZE = 200
    BANNER_TAGS = []
    SERVER_NAME = "Stash Media Server"
    SERVER_ID = ""
    JELLYFIN_VERSION = "10.11.0"
    DEFAULT_PAGE_SIZE = 50
    MAX_PAGE_SIZE = 200
    ENABLE_FILTERS = True
    ENABLE_IMAGE_RESIZE = True
    ENABLE_TAG_FILTERS = False
    ENABLE_ALL_TAGS = False
    REQUIRE_AUTH_FOR_CONFIG = False
    STASH_TIMEOUT = 30
    STASH_RETRIES = 3
    STASH_GRAPHQL_PATH = "/graphql"
    STASH_VERIFY_TLS = False
    LOG_DIR = "."
    LOG_FILE = "stash_jellyfin_proxy.log"
    LOG_LEVEL = "INFO"
    LOG_MAX_SIZE_MB = 10
    LOG_BACKUP_COUNT = 3
    IMAGE_CACHE_MAX_SIZE = 100
    BANNED_IPS = set()
    BAN_THRESHOLD = 10
    BAN_WINDOW_MINUTES = 15
    SERIES_TAG = "Series"
    SERIES_EPISODE_PATTERNS = r"S(\d+)[:\.]?E(\d+), S(\d+)\s+E(\d+), Season\s*(\d+).*?Episode\s*(\d+)"
    PLAYER_PROFILES = []
    GENRE_MODE = "parent_tag"
    GENRE_PARENT_TAG = "GENRE"
    GENRE_TOP_N = 25
    POSTER_CROP_ANCHOR = "center"
    SORT_STRIP_ARTICLES = ["The", "A", "An"]
    OFFICIAL_RATING = "NC-17"
    FILTER_TAGS_MAX = 50
    SCENES_DEFAULT_SORT = "DateCreated"
    STUDIOS_DEFAULT_SORT = "SortName"
    PERFORMERS_DEFAULT_SORT = "SortName"
    GROUPS_DEFAULT_SORT = "SortName"
    TAG_GROUPS_DEFAULT_SORT = "PlayCount"
    SAVED_FILTERS_DEFAULT_SORT = "PlayCount"
    HERO_SOURCE = "recent"
    HERO_MIN_RATING = 75
    GENRE_FILTER_LOGIC = "AND"
    FILTER_TAGS_WALK_HIERARCHY = True
    SEARCH_INCLUDE_SCENES = True
    SEARCH_INCLUDE_PERFORMERS = True
    SEARCH_INCLUDE_STUDIOS = True
    SEARCH_INCLUDE_GROUPS = True

    # ---- Load + migrate + merge ----
    cfg, cfg_defined_keys, cfg_sections = load_config(config_file)

    cfg, cfg_sections, migration_performed, migration_log = run_config_migration(
        config_file, cfg, cfg_defined_keys, cfg_sections
    )
    if migration_performed:
        print(f"Config migrated to v{CURRENT_CONFIG_VERSION}:")
        for line in migration_log:
            print(f"  [migrate] {line}")
    cfg_defined_keys = set(cfg.keys())

    if os.path.isfile(local_config_file) and os.path.abspath(local_config_file) != os.path.abspath(config_file):
        local_cfg, local_keys, local_sections = load_config(local_config_file)
        if local_cfg or local_sections:
            cfg.update(local_cfg)
            cfg_defined_keys.update(local_keys)
            for section_name, section_body in local_sections.items():
                cfg_sections.setdefault(section_name, {}).update(section_body)
            print(f"Loaded local override from {local_config_file}")

    # ---- Apply config values ----
    if cfg:
        STASH_URL = cfg.get("STASH_URL", STASH_URL)
        STASH_API_KEY = cfg.get("STASH_API_KEY", STASH_API_KEY)
        PROXY_BIND = cfg.get("PROXY_BIND", PROXY_BIND)
        PROXY_PORT = int(cfg.get("PROXY_PORT", PROXY_PORT))
        if "UI_PORT" in cfg:
            UI_PORT = int(cfg.get("UI_PORT", UI_PORT))
        SJS_USER = cfg.get("SJS_USER", SJS_USER)
        SJS_PASSWORD = cfg.get("SJS_PASSWORD", SJS_PASSWORD)
        tag_groups_str = cfg.get("TAG_GROUPS", "")
        if tag_groups_str:
            TAG_GROUPS = [t.strip() for t in tag_groups_str.split(",") if t.strip()]
        FAVORITE_TAG = cfg.get("FAVORITE_TAG", FAVORITE_TAG).strip()
        latest_groups_str = cfg.get("LATEST_GROUPS", "")
        if latest_groups_str and latest_groups_str.lower() != "none":
            LATEST_GROUPS = [t.strip() for t in latest_groups_str.split(",") if t.strip()]
        if "BANNER_MODE" in cfg:
            mode = cfg.get("BANNER_MODE", BANNER_MODE).strip().lower()
            BANNER_MODE = mode if mode in ("recent", "tag") else "recent"
        if "BANNER_POOL_SIZE" in cfg:
            try:
                BANNER_POOL_SIZE = max(1, int(cfg.get("BANNER_POOL_SIZE", BANNER_POOL_SIZE)))
            except ValueError:
                pass
        banner_tags_str = cfg.get("BANNER_TAGS", "")
        if banner_tags_str:
            BANNER_TAGS = [t.strip() for t in banner_tags_str.split(",") if t.strip()]
        SERVER_NAME = cfg.get("SERVER_NAME", SERVER_NAME)
        SERVER_ID = cfg.get("SERVER_ID", SERVER_ID)
        JELLYFIN_VERSION = cfg.get("JELLYFIN_VERSION", JELLYFIN_VERSION).strip() or JELLYFIN_VERSION
        if "DEFAULT_PAGE_SIZE" in cfg:
            DEFAULT_PAGE_SIZE = int(cfg.get("DEFAULT_PAGE_SIZE", DEFAULT_PAGE_SIZE))
        if "MAX_PAGE_SIZE" in cfg:
            MAX_PAGE_SIZE = int(cfg.get("MAX_PAGE_SIZE", MAX_PAGE_SIZE))
        if "ENABLE_FILTERS" in cfg:
            ENABLE_FILTERS = parse_bool(cfg.get("ENABLE_FILTERS"), ENABLE_FILTERS)
        if "ENABLE_IMAGE_RESIZE" in cfg:
            ENABLE_IMAGE_RESIZE = parse_bool(cfg.get("ENABLE_IMAGE_RESIZE"), ENABLE_IMAGE_RESIZE)
        if "ENABLE_TAG_FILTERS" in cfg:
            ENABLE_TAG_FILTERS = parse_bool(cfg.get("ENABLE_TAG_FILTERS"), ENABLE_TAG_FILTERS)
        if "ENABLE_ALL_TAGS" in cfg:
            ENABLE_ALL_TAGS = parse_bool(cfg.get("ENABLE_ALL_TAGS"), ENABLE_ALL_TAGS)
        if "REQUIRE_AUTH_FOR_CONFIG" in cfg:
            REQUIRE_AUTH_FOR_CONFIG = parse_bool(cfg.get("REQUIRE_AUTH_FOR_CONFIG"), REQUIRE_AUTH_FOR_CONFIG)
        if "IMAGE_CACHE_MAX_SIZE" in cfg:
            IMAGE_CACHE_MAX_SIZE = int(cfg.get("IMAGE_CACHE_MAX_SIZE", 100))
        if "STASH_TIMEOUT" in cfg:
            STASH_TIMEOUT = int(cfg.get("STASH_TIMEOUT", STASH_TIMEOUT))
        if "STASH_RETRIES" in cfg:
            STASH_RETRIES = int(cfg.get("STASH_RETRIES", STASH_RETRIES))
        if "STASH_GRAPHQL_PATH" in cfg:
            STASH_GRAPHQL_PATH = normalize_path(cfg.get("STASH_GRAPHQL_PATH", STASH_GRAPHQL_PATH))
        if "STASH_VERIFY_TLS" in cfg:
            STASH_VERIFY_TLS = parse_bool(cfg.get("STASH_VERIFY_TLS"), STASH_VERIFY_TLS)
        if "LOG_DIR" in cfg:
            LOG_DIR = cfg.get("LOG_DIR", LOG_DIR)
        if "LOG_FILE" in cfg:
            LOG_FILE = cfg.get("LOG_FILE", LOG_FILE)
        if "LOG_LEVEL" in cfg:
            LOG_LEVEL = cfg.get("LOG_LEVEL", LOG_LEVEL).upper()
        if "LOG_MAX_SIZE_MB" in cfg:
            LOG_MAX_SIZE_MB = int(cfg.get("LOG_MAX_SIZE_MB", LOG_MAX_SIZE_MB))
        if "LOG_BACKUP_COUNT" in cfg:
            LOG_BACKUP_COUNT = int(cfg.get("LOG_BACKUP_COUNT", LOG_BACKUP_COUNT))
        if "BANNED_IPS" in cfg:
            banned_str = cfg.get("BANNED_IPS", "")
            if banned_str:
                BANNED_IPS = set(ip.strip() for ip in banned_str.split(",") if ip.strip())
        if "BAN_THRESHOLD" in cfg:
            BAN_THRESHOLD = int(cfg.get("BAN_THRESHOLD", BAN_THRESHOLD))
        if "BAN_WINDOW_MINUTES" in cfg:
            BAN_WINDOW_MINUTES = int(cfg.get("BAN_WINDOW_MINUTES", BAN_WINDOW_MINUTES))
        if "series_tag" in cfg:
            SERIES_TAG = cfg.get("series_tag", SERIES_TAG).strip()
        if "series_episode_patterns" in cfg:
            SERIES_EPISODE_PATTERNS = cfg.get("series_episode_patterns", SERIES_EPISODE_PATTERNS)
        if "genre_mode" in cfg:
            mode = cfg.get("genre_mode", "parent_tag").strip().lower()
            GENRE_MODE = mode if mode in ("all_tags", "parent_tag", "top_n") else "parent_tag"
        if "genre_parent_tag" in cfg:
            GENRE_PARENT_TAG = cfg.get("genre_parent_tag", GENRE_PARENT_TAG).strip()
        if "genre_top_n" in cfg:
            try:
                GENRE_TOP_N = max(1, int(cfg.get("genre_top_n", GENRE_TOP_N)))
            except ValueError:
                pass
        if "poster_crop_anchor" in cfg:
            anchor = cfg.get("poster_crop_anchor", "center").strip().lower()
            POSTER_CROP_ANCHOR = anchor if anchor in ("center", "left", "right") else "center"
        if "sort_strip_articles" in cfg:
            raw = cfg.get("sort_strip_articles", "")
            SORT_STRIP_ARTICLES = [a.strip() for a in raw.split(",") if a.strip()]
        if "official_rating" in cfg:
            rating = cfg.get("official_rating", OFFICIAL_RATING).strip()
            OFFICIAL_RATING = rating if rating else OFFICIAL_RATING
        if "filter_tags_max" in cfg:
            try:
                FILTER_TAGS_MAX = max(1, int(cfg.get("filter_tags_max", FILTER_TAGS_MAX)))
            except ValueError:
                pass
        SCENES_DEFAULT_SORT = cfg.get("scenes_default_sort", SCENES_DEFAULT_SORT).strip() or SCENES_DEFAULT_SORT
        STUDIOS_DEFAULT_SORT = cfg.get("studios_default_sort", STUDIOS_DEFAULT_SORT).strip() or STUDIOS_DEFAULT_SORT
        PERFORMERS_DEFAULT_SORT = cfg.get("performers_default_sort", PERFORMERS_DEFAULT_SORT).strip() or PERFORMERS_DEFAULT_SORT
        GROUPS_DEFAULT_SORT = cfg.get("groups_default_sort", GROUPS_DEFAULT_SORT).strip() or GROUPS_DEFAULT_SORT
        TAG_GROUPS_DEFAULT_SORT = cfg.get("tag_groups_default_sort", TAG_GROUPS_DEFAULT_SORT).strip() or TAG_GROUPS_DEFAULT_SORT
        SAVED_FILTERS_DEFAULT_SORT = cfg.get("saved_filters_default_sort", SAVED_FILTERS_DEFAULT_SORT).strip() or SAVED_FILTERS_DEFAULT_SORT
        if "hero_source" in cfg:
            raw = cfg.get("hero_source", "recent").strip().lower()
            if raw in ("recent", "random", "favorites", "top_rated", "recently_watched"):
                HERO_SOURCE = raw
        if "hero_min_rating" in cfg:
            try:
                HERO_MIN_RATING = max(0, min(100, int(cfg.get("hero_min_rating", HERO_MIN_RATING))))
            except ValueError:
                pass
        if "genre_filter_logic" in cfg:
            raw = cfg.get("genre_filter_logic", "AND").strip().upper()
            GENRE_FILTER_LOGIC = "OR" if raw == "OR" else "AND"
        if "filter_tags_walk_hierarchy" in cfg:
            FILTER_TAGS_WALK_HIERARCHY = cfg.get("filter_tags_walk_hierarchy", "true").strip().lower() in ("true", "yes", "1", "on")
        for cfg_key, var_name in (
            ("search_include_scenes", "SEARCH_INCLUDE_SCENES"),
            ("search_include_performers", "SEARCH_INCLUDE_PERFORMERS"),
            ("search_include_studios", "SEARCH_INCLUDE_STUDIOS"),
            ("search_include_groups", "SEARCH_INCLUDE_GROUPS"),
        ):
            if cfg_key in cfg:
                val = cfg.get(cfg_key, "true").strip().lower() in ("true", "yes", "1", "on")
                if var_name == "SEARCH_INCLUDE_SCENES":
                    SEARCH_INCLUDE_SCENES = val
                elif var_name == "SEARCH_INCLUDE_PERFORMERS":
                    SEARCH_INCLUDE_PERFORMERS = val
                elif var_name == "SEARCH_INCLUDE_STUDIOS":
                    SEARCH_INCLUDE_STUDIOS = val
                elif var_name == "SEARCH_INCLUDE_GROUPS":
                    SEARCH_INCLUDE_GROUPS = val
        print(f"Loaded config from {config_file}")
    else:
        cfg_defined_keys = set()
        print(f"Warning: Config file {config_file} not found or empty. Using defaults/env vars.")

    # ---- Env overrides (always win over file) ----
    env_overrides = []

    if os.getenv("STASH_URL"):
        STASH_URL = os.getenv("STASH_URL")
        env_overrides.append("STASH_URL")
    if os.getenv("STASH_API_KEY"):
        STASH_API_KEY = os.getenv("STASH_API_KEY")
        env_overrides.append("STASH_API_KEY")
    if os.getenv("PROXY_BIND"):
        PROXY_BIND = os.getenv("PROXY_BIND")
        if os.getenv("PROXY_BIND") != _DOCKER_ENV_DEFAULTS["PROXY_BIND"]:
            env_overrides.append("PROXY_BIND")
    if os.getenv("PROXY_PORT"):
        PROXY_PORT = int(os.getenv("PROXY_PORT"))
        if os.getenv("PROXY_PORT") != _DOCKER_ENV_DEFAULTS["PROXY_PORT"]:
            env_overrides.append("PROXY_PORT")
    if os.getenv("UI_PORT"):
        UI_PORT = int(os.getenv("UI_PORT"))
        if os.getenv("UI_PORT") != _DOCKER_ENV_DEFAULTS["UI_PORT"]:
            env_overrides.append("UI_PORT")
    if os.getenv("LOG_DIR"):
        LOG_DIR = os.getenv("LOG_DIR")
        if os.getenv("LOG_DIR") != _DOCKER_ENV_DEFAULTS["LOG_DIR"]:
            env_overrides.append("LOG_DIR")
    if os.getenv("SJS_USER"):
        SJS_USER = os.getenv("SJS_USER")
        env_overrides.append("SJS_USER")
    if os.getenv("SJS_PASSWORD"):
        SJS_PASSWORD = os.getenv("SJS_PASSWORD")
        env_overrides.append("SJS_PASSWORD")
    if os.getenv("SERVER_ID"):
        SERVER_ID = os.getenv("SERVER_ID")
        env_overrides.append("SERVER_ID")
    if os.getenv("JELLYFIN_VERSION"):
        JELLYFIN_VERSION = os.getenv("JELLYFIN_VERSION")
        env_overrides.append("JELLYFIN_VERSION")
    if os.getenv("REQUIRE_AUTH_FOR_CONFIG"):
        REQUIRE_AUTH_FOR_CONFIG = os.getenv("REQUIRE_AUTH_FOR_CONFIG", "").lower() in ("true", "yes", "1", "on")
        env_overrides.append("REQUIRE_AUTH_FOR_CONFIG")
    if os.getenv("STASH_GRAPHQL_PATH"):
        STASH_GRAPHQL_PATH = normalize_path(os.getenv("STASH_GRAPHQL_PATH"))
        env_overrides.append("STASH_GRAPHQL_PATH")
    if os.getenv("STASH_VERIFY_TLS"):
        STASH_VERIFY_TLS = os.getenv("STASH_VERIFY_TLS", "").lower() in ("true", "yes", "1", "on")
        env_overrides.append("STASH_VERIFY_TLS")

    if env_overrides:
        print(f"  Env overrides: {', '.join(env_overrides)}")

    # ---- Print effective config ----
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
    banner_suffix = f", tags=[{', '.join(BANNER_TAGS)}]" if BANNER_TAGS else ""
    print(f"  Banner: mode={BANNER_MODE}, pool={BANNER_POOL_SIZE}{banner_suffix}")
    print(f"  Series tag: {SERIES_TAG}")

    # ---- Load player profiles from [player.*] sections ----
    from stash_jellyfin_proxy.players.profiles import load_profiles
    PLAYER_PROFILES = load_profiles(cfg_sections or {})
    print(f"  Player profiles: {', '.join(p.name for p in PLAYER_PROFILES)}")
    if not any(p.name == "default" for p in PLAYER_PROFILES):
        print("  WARNING: no [player.default] section found — using hardcoded fallback")

    # ---- Auto-generate SERVER_ID / normalize legacy format ----
    if not SERVER_ID:
        SERVER_ID = generate_server_id()
        print(f"  Generated new Server ID: {SERVER_ID}")
        try:
            save_server_id_to_config(config_file, SERVER_ID)
            print(f"  Saved Server ID to {config_file}")
            cfg_defined_keys.add("SERVER_ID")
        except Exception as e:
            print(f"  Warning: Could not save Server ID to config: {e}")
            print("  Server ID will be regenerated on next restart unless saved manually.")
    else:
        normalized = normalize_server_id(SERVER_ID)
        if normalized != SERVER_ID:
            print(f"  Upgraded Server ID to UUID format: {normalized}")
            SERVER_ID = normalized
            try:
                save_server_id_to_config(config_file, SERVER_ID)
                print(f"  Saved updated Server ID to {config_file}")
            except Exception as e:
                print(f"  Warning: Could not save updated Server ID to config: {e}")

    # ---- Auto-generate ACCESS_TOKEN ----
    ACCESS_TOKEN = cfg.get("ACCESS_TOKEN", "") if cfg else ""
    if not ACCESS_TOKEN:
        ACCESS_TOKEN = str(uuid.uuid4())
        print(f"  Generated new Access Token")
        try:
            save_config_value(
                config_file, "ACCESS_TOKEN", ACCESS_TOKEN,
                "Persistent access token for client sessions (auto-generated)",
            )
            print(f"  Saved Access Token to {config_file}")
        except Exception as e:
            print(f"  Warning: Could not save Access Token to config: {e}")

    # ---- Derive USER_ID (stable per-server/per-user UUID) ----
    _uuid_padded = SERVER_ID.replace("-", "").ljust(32, "0")[:32]
    USER_ID = str(uuid.uuid5(uuid.UUID(_uuid_padded), SJS_USER or "user"))

    # ---- Publish to stash_jellyfin_proxy.runtime ----
    runtime.publish(
        STASH_URL=STASH_URL,
        STASH_API_KEY=STASH_API_KEY,
        STASH_GRAPHQL_PATH=STASH_GRAPHQL_PATH,
        STASH_VERIFY_TLS=STASH_VERIFY_TLS,
        STASH_TIMEOUT=STASH_TIMEOUT,
        STASH_RETRIES=STASH_RETRIES,
        STASH_SESSION=None,
        PROXY_BIND=PROXY_BIND,
        PROXY_PORT=PROXY_PORT,
        UI_PORT=UI_PORT,
        SERVER_NAME=SERVER_NAME,
        SERVER_ID=SERVER_ID,
        SJS_USER=SJS_USER,
        SJS_PASSWORD=SJS_PASSWORD,
        ACCESS_TOKEN=ACCESS_TOKEN,
        TAG_GROUPS=TAG_GROUPS,
        FAVORITE_TAG=FAVORITE_TAG,
        LATEST_GROUPS=LATEST_GROUPS,
        BANNER_MODE=BANNER_MODE,
        BANNER_POOL_SIZE=BANNER_POOL_SIZE,
        BANNER_TAGS=BANNER_TAGS,
        ENABLE_FILTERS=ENABLE_FILTERS,
        ENABLE_IMAGE_RESIZE=ENABLE_IMAGE_RESIZE,
        ENABLE_TAG_FILTERS=ENABLE_TAG_FILTERS,
        ENABLE_ALL_TAGS=ENABLE_ALL_TAGS,
        REQUIRE_AUTH_FOR_CONFIG=REQUIRE_AUTH_FOR_CONFIG,
        DEFAULT_PAGE_SIZE=DEFAULT_PAGE_SIZE,
        MAX_PAGE_SIZE=MAX_PAGE_SIZE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE={},
        LOG_DIR=LOG_DIR,
        LOG_FILE=LOG_FILE,
        LOG_LEVEL=LOG_LEVEL,
        LOG_MAX_SIZE_MB=LOG_MAX_SIZE_MB,
        LOG_BACKUP_COUNT=LOG_BACKUP_COUNT,
        BANNED_IPS=BANNED_IPS,
        BAN_THRESHOLD=BAN_THRESHOLD,
        BAN_WINDOW_MINUTES=BAN_WINDOW_MINUTES,
        CONFIG_FILE=config_file,
        LOCAL_CONFIG_FILE=local_config_file,
        config=cfg,
        config_defined_keys=cfg_defined_keys,
        config_sections=cfg_sections,
        MIGRATION_PERFORMED=migration_performed,
        MIGRATION_LOG=migration_log,
        JELLYFIN_VERSION=JELLYFIN_VERSION,
        USER_ID=USER_ID,
        env_overrides=env_overrides,
        SERIES_TAG=SERIES_TAG,
        SERIES_EPISODE_PATTERNS=SERIES_EPISODE_PATTERNS,
        PLAYER_PROFILES=PLAYER_PROFILES,
        GENRE_MODE=GENRE_MODE,
        GENRE_PARENT_TAG=GENRE_PARENT_TAG,
        GENRE_TOP_N=GENRE_TOP_N,
        POSTER_CROP_ANCHOR=POSTER_CROP_ANCHOR,
        SORT_STRIP_ARTICLES=SORT_STRIP_ARTICLES,
        OFFICIAL_RATING=OFFICIAL_RATING,
        FILTER_TAGS_MAX=FILTER_TAGS_MAX,
        SCENES_DEFAULT_SORT=SCENES_DEFAULT_SORT,
        STUDIOS_DEFAULT_SORT=STUDIOS_DEFAULT_SORT,
        PERFORMERS_DEFAULT_SORT=PERFORMERS_DEFAULT_SORT,
        GROUPS_DEFAULT_SORT=GROUPS_DEFAULT_SORT,
        TAG_GROUPS_DEFAULT_SORT=TAG_GROUPS_DEFAULT_SORT,
        SAVED_FILTERS_DEFAULT_SORT=SAVED_FILTERS_DEFAULT_SORT,
        HERO_SOURCE=HERO_SOURCE,
        HERO_MIN_RATING=HERO_MIN_RATING,
        GENRE_FILTER_LOGIC=GENRE_FILTER_LOGIC,
        FILTER_TAGS_WALK_HIERARCHY=FILTER_TAGS_WALK_HIERARCHY,
        SEARCH_INCLUDE_SCENES=SEARCH_INCLUDE_SCENES,
        SEARCH_INCLUDE_PERFORMERS=SEARCH_INCLUDE_PERFORMERS,
        SEARCH_INCLUDE_STUDIOS=SEARCH_INCLUDE_STUDIOS,
        SEARCH_INCLUDE_GROUPS=SEARCH_INCLUDE_GROUPS,
    )
