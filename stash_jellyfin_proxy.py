#!/usr/bin/env python3
import os
import sys
import json
import logging
import asyncio
import signal
import uuid
import argparse
import time
from typing import Optional, List, Dict, Any, Tuple
from logging.handlers import SysLogHandler

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

# --- Configuration Loading ---
# Config file location: same directory as script, or specified path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "stash_jellyfin_proxy.conf")

# Default Configuration (can be overridden by config file)
STASH_URL = "https://stash.feldorn.com"
STASH_API_KEY = ""  # Real Stash API key from Settings -> Security -> API Key
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
# User credentials for Infuse authentication
SJS_USER = "chris"
SJS_PASSWORD = "infuse12345"

# Tag groups - comma-separated list of tag names to show as top-level folders
TAG_GROUPS = []  # e.g., ["Favorites", "VR", "4K"]

# Latest groups - controls which libraries show on Infuse home page
# "Scenes" = all scenes, other entries must match TAG_GROUPS entries
LATEST_GROUPS = ["Scenes"]  # e.g., ["Scenes", "VR", "Favorites"]

# Load Config - parses config file with KEY = "value" or KEY="value" format
def load_config(filepath):
    """Load configuration from a shell-style config file."""
    config = {}
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
        except Exception as e:
            print(f"Error loading config file {filepath}: {e}", file=sys.stderr)
    return config

_config = load_config(CONFIG_FILE)
if _config:
    STASH_URL = _config.get("STASH_URL", STASH_URL)
    STASH_API_KEY = _config.get("STASH_API_KEY", STASH_API_KEY)
    PROXY_BIND = _config.get("PROXY_BIND", PROXY_BIND)
    PROXY_PORT = int(_config.get("PROXY_PORT", PROXY_PORT))
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
    print(f"Loaded config from {CONFIG_FILE}")
    print(f"  User: {SJS_USER}")
    print(f"  Stash URL: {STASH_URL}")
    print(f"  Proxy: {PROXY_BIND}:{PROXY_PORT}")
    if STASH_API_KEY:
        print(f"  API key: configured ({len(STASH_API_KEY)} chars)")
    else:
        print("WARNING: STASH_API_KEY not set in config file!")
        print("  Images will not load. Add STASH_API_KEY to your config file.")
        print("  Get your API key from: Stash -> Settings -> Security -> API Key")
    if TAG_GROUPS:
        print(f"  Tag groups: {', '.join(TAG_GROUPS)}")
    if LATEST_GROUPS:
        print(f"  Latest groups: {', '.join(LATEST_GROUPS)}")
else:
    print(f"Warning: Config file {CONFIG_FILE} not found or empty. Using defaults/env vars.")
    STASH_URL = os.getenv("STASH_URL", STASH_URL)
    STASH_API_KEY = os.getenv("STASH_API_KEY", STASH_API_KEY)
    PROXY_BIND = os.getenv("PROXY_BIND", PROXY_BIND)
    PROXY_PORT = int(os.getenv("PROXY_PORT", PROXY_PORT))
    SJS_USER = os.getenv("SJS_USER", SJS_USER)
    SJS_PASSWORD = os.getenv("SJS_PASSWORD", SJS_PASSWORD)

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
    </svg>"""
}

# --- Logging Setup ---
# Configure root logger to output to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("stash-jellyfin-proxy")

# --- Middleware for Request Logging ---
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start_time = time.time()
        client_host = request.client.host if request.client else "unknown"
        logger.info(f"INCOMING: {request.method} {request.url.path} from {client_host}")
        
        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            logger.info(f"COMPLETED: {response.status_code} in {process_time:.4f}s")
            return response
        except Exception as e:
            logger.error(f"FAILED: {request.url.path} - {str(e)}", exc_info=True)
            return JSONResponse({"error": "Internal Server Error"}, status_code=500)

# --- Stash GraphQL Client ---
GRAPHQL_URL = f"{STASH_URL}/graphql-local" if not STASH_URL.endswith("/graphql-local") else STASH_URL

# Create a persistent session with authentication
STASH_SESSION = None

def get_stash_session():
    """Get or create a Stash session with ApiKey authentication."""
    global STASH_SESSION
    
    if STASH_SESSION is not None:
        return STASH_SESSION
    
    STASH_SESSION = requests.Session()
    
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
        logger.info(f"✅ Connected to Stash! Version: {v}")
        return True
    except Exception as e:
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

def stash_query(query: str, variables: Dict[str, Any] = None) -> Dict[str, Any]:
    try:
        session = get_stash_session()
        resp = session.post(GRAPHQL_URL, json={"query": query, "variables": variables or {}}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Stash API Query Error: {e}")
        return {"errors": [str(e)]}

def stash_get_saved_filters(mode: str) -> List[Dict[str, Any]]:
    """Get saved filters from Stash for a specific mode (SCENES, PERFORMERS, STUDIOS, GROUPS)."""
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
    logger.debug(f"Found {len(filters)} saved filters for mode {mode}")
    return filters

# Filter mode mapping: library parent_id -> Stash FilterMode
FILTER_MODE_MAP = {
    "root-scenes": "SCENES",
    "root-performers": "PERFORMERS",
    "root-studios": "STUDIOS",
    "root-groups": "GROUPS",
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
SERVER_ID = "a1b2c3d4e5f6a1b2c3d4e5f6"
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
    title = scene.get("title") or scene.get("code") or f"Scene {raw_id}"
    date = scene.get("date")
    files = scene.get("files", [])
    path = files[0].get("path") if files else ""
    duration = files[0].get("duration", 0) if files else 0
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
    logger.info("Providing System Info")
    return JSONResponse({
        "ServerName": "Stash Proxy",
        "Version": "10.8.13", # Updated to a newer stable version
        "Id": SERVER_ID,
        "OperatingSystem": "Linux",
        "SupportsLibraryMonitor": False,
        "WebSocketPortNumber": PROXY_PORT,
        "CompletedInstallations": [{"Guid": SERVER_ID, "Name": "Stash Proxy"}],
        "CanSelfRestart": False,
        "CanLaunchWebBrowser": False,
        "LocalAddress": f"http://{PROXY_BIND}:{PROXY_PORT}"
    })

async def endpoint_public_info(request):
    return JSONResponse({
        "LocalAddress": f"http://{PROXY_BIND}:{PROXY_PORT}",
        "ServerName": "Stash Proxy",
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
    
    # Accept config password
    if pw == SJS_PASSWORD:
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
    
    # Add tag group folders
    for tag_name in TAG_GROUPS:
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
    
    # Add tag group folders
    for tag_name in TAG_GROUPS:
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
    
    logger.info(f"Latest items request - ParentId: {parent_id}, Limit: {limit}")
    
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
        logger.info(f"Skipping latest for {parent_id} (not in LATEST_GROUPS)")
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
                logger.info(f"Tag '{tag_name}' latest: {len(scenes)} scenes")
                for s in scenes:
                    items.append(format_jellyfin_item(s, parent_id=parent_id))
    
    logger.info(f"Returning {len(items)} latest items for {parent_id}")
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

async def endpoint_items(request):
    user_id = request.path_params.get("user_id")
    # Handle both ParentId and parentId (Infuse uses lowercase)
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    ids = request.query_params.get("Ids") or request.query_params.get("ids")
    
    # Pagination parameters
    start_index = int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0)
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 50)
    
    # Sort parameters
    sort_field, sort_direction = get_stash_sort_params(request)
    
    # Check for PersonIds parameter (Infuse uses this when clicking on a person)
    person_ids = request.query_params.get("PersonIds") or request.query_params.get("personIds")
    
    # Debug: Log ALL query params to understand what Infuse is sending
    all_params = dict(request.query_params)
    logger.info(f"Items endpoint - ALL PARAMS: {all_params}")
    logger.info(f"Items endpoint - ParentId: {parent_id}, Ids: {ids}, PersonIds: {person_ids}, StartIndex: {start_index}, Limit: {limit}, Sort: {sort_field} {sort_direction}")
    
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
        
        logger.info(f"PersonIds filter: fetching scenes for performer {performer_id}")
        
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
        logger.info(f"PersonIds filter: returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=f"person-{performer_id}"))
    
    elif parent_id and parent_id.startswith("filters-"):
        # List saved filters for a specific mode (filters-scenes, filters-performers, etc.)
        filter_mode = parent_id.replace("filters-", "").upper()
        saved_filters = stash_get_saved_filters(filter_mode)
        total_count = len(saved_filters)
        
        logger.info(f"Listing {total_count} saved filters for mode {filter_mode}")
        
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
                
                logger.info(f"Applying saved filter '{saved_filter.get('name')}' (id={filter_id}, mode={filter_mode})")
                logger.info(f"Raw object_filter type: {type(object_filter)}, value: {object_filter}")
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
                    count_res = stash_query(count_q, {"scene_filter": object_filter})
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
                        "scene_filter": object_filter,
                        "page": page, 
                        "per_page": limit, 
                        "sort": sort_field, 
                        "direction": sort_direction
                    })
                    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.info(f"Saved filter returned {len(scenes)} scenes (page {page}, total {total_count})")
                    for s in scenes:
                        items.append(format_jellyfin_item(s, parent_id=parent_id))
                
                elif filter_mode == "PERFORMERS":
                    # Count performers with filter
                    count_q = """query CountPerformers($performer_filter: PerformerFilterType) { 
                        findPerformers(performer_filter: $performer_filter) { count } 
                    }"""
                    count_res = stash_query(count_q, {"performer_filter": object_filter})
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
                    res = stash_query(q, {"performer_filter": object_filter, "page": page, "per_page": limit})
                    performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])
                    logger.info(f"Saved filter returned {len(performers)} performers (page {page}, total {total_count})")
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
                    count_res = stash_query(count_q, {"studio_filter": object_filter})
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
                    res = stash_query(q, {"studio_filter": object_filter, "page": page, "per_page": limit})
                    studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
                    logger.info(f"Saved filter returned {len(studios)} studios (page {page}, total {total_count})")
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
                    count_res = stash_query(count_q, {"group_filter": object_filter})
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
                    res = stash_query(q, {"group_filter": object_filter, "page": page, "per_page": limit})
                    groups = res.get("data", {}).get("findGroups", {}).get("groups", [])
                    logger.info(f"Saved filter returned {len(groups)} groups (page {page}, total {total_count})")
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
                
                else:
                    logger.warning(f"Unsupported filter mode: {filter_mode}")
            else:
                logger.warning(f"Saved filter not found: {filter_id}")
    
    elif parent_id == "root-scenes":
        # Calculate page number from startIndex (Stash uses 1-indexed pages)
        page = (start_index // limit) + 1
        
        # First get total count
        count_q = """query { findScenes { count } }"""
        count_res = stash_query(count_q)
        scene_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
        
        # Check if there are saved filters for scenes
        saved_filters = stash_get_saved_filters("SCENES")
        has_filters = len(saved_filters) > 0
        
        # On first page, add FILTERS folder at the top if there are saved filters
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-scenes"))
            # Adjust total count to include FILTERS folder
            total_count = scene_count + 1
        else:
            total_count = scene_count
        
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
        
        # Check if there are saved filters for studios
        saved_filters = stash_get_saved_filters("STUDIOS")
        has_filters = len(saved_filters) > 0
        
        # On first page, add FILTERS folder at the top if there are saved filters
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-studios"))
            total_count = studio_count + 1
        else:
            total_count = studio_count
        
        # Calculate page
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
        
        # Check if there are saved filters for performers
        saved_filters = stash_get_saved_filters("PERFORMERS")
        has_filters = len(saved_filters) > 0
        
        # On first page, add FILTERS folder at the top if there are saved filters
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-performers"))
            total_count = performer_count + 1
        else:
            total_count = performer_count
        
        # Calculate page
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
        
        # Check if there are saved filters for groups
        saved_filters = stash_get_saved_filters("GROUPS")
        has_filters = len(saved_filters) > 0
        
        # On first page, add FILTERS folder at the top if there are saved filters
        if start_index == 0 and has_filters:
            items.append(format_filters_folder("root-groups"))
            total_count = group_count + 1
        else:
            total_count = group_count
        
        # Calculate page
        page = (start_index // limit) + 1
        
        # Query for movies - always try to get the image, endpoint will handle failures
        q = """query FindMovies($page: Int!, $per_page: Int!) { 
            findMovies(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) { 
                movies { id name scene_count } 
            } 
        }"""
        res = stash_query(q, {"page": page, "per_page": limit})
        for m in res.get("data", {}).get("findMovies", {}).get("movies", []):
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
    
    elif parent_id and parent_id.startswith("tag-"):
        # Tag-based folder: find scenes with this tag
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
                logger.info(f"Tag '{tag_name}' (id={tag_id}) returned {len(scenes)} scenes (page {page}, total {total_count})")
                for s in scenes:
                    items.append(format_jellyfin_item(s, parent_id=parent_id))
            else:
                logger.warning(f"Tag '{tag_name}' not found in Stash")
        else:
            logger.warning(f"Tag slug '{tag_slug}' not found in TAG_GROUPS config")
            
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
        if item_id.startswith("performer-"):
            performer_id = item_id.replace("performer-", "")
        else:
            performer_id = item_id.replace("person-", "")
        q = """query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path scene_count } }"""
        res = stash_query(q, {"id": performer_id})
        performer = res.get("data", {}).get("findPerformer", {})
        
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
    
    elif item_id.startswith("tag-"):
        # Tag-based folder
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
    # Accept all session reports silently
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
    
    logger.info(f"PlaybackInfo for {item_id}: {len(captions)} subtitles")
    
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
    """Proxy video stream from Stash with proper authentication."""
    item_id = request.path_params.get("item_id")
    numeric_id = get_numeric_id(item_id)
    stash_stream_url = f"{STASH_URL}/scene/{numeric_id}/stream"
    
    logger.info(f"Proxying stream for {item_id} from {stash_stream_url}")
    
    # Build extra headers
    extra_headers = {}
    if "range" in request.headers:
        extra_headers["Range"] = request.headers["range"]
    
    try:
        data, content_type, resp_headers = fetch_from_stash(stash_stream_url, extra_headers, timeout=120, stream=True)
        
        from starlette.responses import Response
        headers = {"Accept-Ranges": "bytes"}
        if "Content-Length" in resp_headers:
            headers["Content-Length"] = resp_headers["Content-Length"]
        if "Content-Range" in resp_headers:
            headers["Content-Range"] = resp_headers["Content-Range"]
        
        status_code = 206 if "Content-Range" in resp_headers else 200
        logger.info(f"Stream response: {len(data)} bytes, type={content_type}")
        return Response(content=data, media_type=content_type, headers=headers, status_code=status_code)
        
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
        
        logger.info(f"Proxying subtitle for {item_id} index {subtitle_index} from {stash_caption_url}")
        
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
        
        logger.info(f"Subtitle response: {len(data)} bytes, type={content_type}")
        from starlette.responses import Response
        return Response(content=data, media_type=content_type, headers={
            "Content-Disposition": f'attachment; filename="subtitle.{caption_type}"'
        })
        
    except Exception as e:
        logger.error(f"Subtitle proxy error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

def generate_text_icon(text: str, width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Generate a portrait 2:3 PNG icon with text label (matches Infuse folder tiles)."""
    if not PILLOW_AVAILABLE:
        # Return a simple SVG with text as fallback
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
            <rect width="400" height="600" fill="#1a1a2e"/>
            <text x="200" y="320" text-anchor="middle" fill="#4a90d9" font-size="48" font-family="sans-serif">{text}</text>
        </svg>'''
        return svg.encode('utf-8'), "image/svg+xml"
    
    try:
        from PIL import ImageDraw, ImageFont
        
        # Create portrait image with dark background (2:3 aspect for Infuse folder tiles)
        img = Image.new('RGB', (width, height), (26, 26, 46))
        draw = ImageDraw.Draw(img)
        
        # Text color (Stash-like blue)
        text_color = (74, 144, 217)  # #4a90d9
        
        # Try to find a good font size that fits the text
        # Start with a large size and reduce if needed
        max_width = width - 40  # Leave 20px padding on each side
        font_size = 72
        font = None
        
        # Try to load a nice font, fall back to default
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
        
        for font_path in font_paths:
            try:
                font = ImageFont.truetype(font_path, font_size)
                break
            except (IOError, OSError):
                continue
        
        if font is None:
            # Use default font
            font = ImageFont.load_default()
            font_size = 20  # Default font is small
        
        # Reduce font size until text fits
        while font_size > 20:
            try:
                font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
            except:
                font = ImageFont.load_default()
                break
            
            # Get text bounding box
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            
            if text_width <= max_width:
                break
            font_size -= 4
        
        # Calculate position to center text
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        x = (width - text_width) // 2
        y = (height - text_height) // 2
        
        # Draw text
        draw.text((x, y), text, fill=text_color, font=font)
        
        # Save as PNG
        output = io.BytesIO()
        img.save(output, format='PNG')
        return output.getvalue(), "image/png"
        
    except Exception as e:
        logger.warning(f"Text icon generation failed: {e}")
        # Simple SVG fallback
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600" width="400" height="600">
            <rect width="400" height="600" fill="#1a1a2e"/>
            <text x="200" y="320" text-anchor="middle" fill="#4a90d9" font-size="48" font-family="sans-serif">{text}</text>
        </svg>'''
        return svg.encode('utf-8'), "image/svg+xml"

def generate_menu_icon(icon_type: str, width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Generate a portrait 2:3 PNG menu icon with text label (matches Infuse folder tiles)."""
    # Map icon types to display names
    icon_names = {
        "root-scenes": "Scenes",
        "root-studios": "Studios", 
        "root-performers": "Performers",
        "root-groups": "Groups",
        "root-tag": "Tags",
    }
    
    text = icon_names.get(icon_type, icon_type.replace("root-", "").replace("-", " ").title())
    return generate_text_icon(text, width, height)

def generate_placeholder_icon(item_type: str = "group", width: int = 400, height: int = 600) -> Tuple[bytes, str]:
    """Generate a placeholder icon for items without images."""
    if not PILLOW_AVAILABLE:
        # Return a simple dark image
        return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82', "image/png"
    
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
    
    # Handle menu icons for root folders
    if item_id in MENU_ICONS:
        # Generate PNG icon using Pillow drawing
        img_data, content_type = generate_menu_icon(item_id)
        logger.info(f"Serving menu icon for {item_id}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers={"Cache-Control": "max-age=86400"})
    
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
        logger.info(f"Serving text icon for tag folder: {display_name}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers={"Cache-Control": "max-age=86400"})
    
    # Handle FILTERS folder icons
    if item_id.startswith("filters-"):
        img_data, content_type = generate_text_icon("FILTERS")
        logger.info(f"Serving text icon for filters folder: {item_id}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers={"Cache-Control": "max-age=86400"})
    
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
            img_data, content_type = generate_text_icon(filter_name)
            logger.info(f"Serving text icon for saved filter: {filter_name}")
            from starlette.responses import Response
            return Response(content=img_data, media_type=content_type, headers={"Cache-Control": "max-age=86400"})
    
    # Check query params for placeholder flag (set when group has no front_image)
    image_tag = request.query_params.get("tag", "")
    if image_tag == "placeholder" and item_id.startswith("group-"):
        # Generate placeholder icon for groups without images
        img_data, content_type = generate_placeholder_icon("group")
        logger.info(f"Serving placeholder icon for {item_id}")
        from starlette.responses import Response
        return Response(content=img_data, media_type=content_type, headers={"Cache-Control": "max-age=86400"})
    
    # Determine image URL and whether to resize based on item type
    needs_portrait_resize = False
    is_group_image = False  # Flag to enable SVG placeholder detection for groups
    if item_id.startswith("studio-"):
        numeric_id = item_id.replace("studio-", "")
        stash_img_url = f"{STASH_URL}/studio/{numeric_id}/image"
        needs_portrait_resize = True  # Studio logos need portrait padding for Infuse tiles
    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        if item_id.startswith("performer-"):
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
    
    logger.info(f"Proxying image for {item_id} from {stash_img_url}")
    
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
                logger.info(f"Empty/small response for group image, using placeholder: {item_id}")
                img_data, ct = generate_placeholder_icon("group")
                from starlette.responses import Response
                return Response(content=img_data, media_type=ct, headers=cache_headers)
        
        # Check if we got an image content type
        if content_type and not content_type.startswith("image/"):
            if item_id.startswith("group-"):
                logger.info(f"Non-image response for group ({content_type}), using placeholder: {item_id}")
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
                        logger.info(f"GraphQL fallback: fetching from {gql_img_url}")
                        data, content_type, _ = fetch_from_stash(gql_img_url, extra_headers=image_headers, timeout=30)
                        if data and len(data) > 1000 and content_type != "image/svg+xml":
                            logger.info(f"GraphQL fallback successful: {len(data)} bytes, type={content_type}")
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
        if needs_portrait_resize and PILLOW_AVAILABLE:
            data, content_type = pad_image_to_portrait(data, target_width=400, target_height=600)
            logger.info(f"Resized studio image to 400x600 portrait (2:3)")
            
            # Cache the resized image
            if len(IMAGE_CACHE) >= IMAGE_CACHE_MAX_SIZE:
                # Remove oldest entry (simple FIFO)
                oldest_key = next(iter(IMAGE_CACHE))
                del IMAGE_CACHE[oldest_key]
            IMAGE_CACHE[cache_key] = (data, content_type)
        
        from starlette.responses import Response
        logger.info(f"Image response: {len(data)} bytes, type={content_type}")
        return Response(content=data, media_type=content_type, headers=cache_headers)
        
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        from starlette.responses import Response
        
        # For groups, return a placeholder icon instead of transparent pixel
        if item_id.startswith("group-"):
            img_data, content_type = generate_placeholder_icon("group")
            logger.info(f"Serving placeholder icon for failed group image: {item_id}")
            return Response(content=img_data, media_type=content_type, headers=cache_headers)
        
        # Return transparent 1x1 PNG as fallback for other types
        return Response(content=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82', media_type='image/png', headers=cache_headers)

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
    Route("/Users/AuthenticateByName", endpoint_authenticate_by_name, methods=["POST"]),
    Route("/Users/{user_id}", endpoint_user_by_id),
    Route("/Users/{user_id}/Views", endpoint_user_views),
    Route("/Users/{user_id}/Items/Latest", endpoint_latest_items),
    Route("/Users/{user_id}/GroupingOptions", endpoint_grouping_options),
    Route("/Library/VirtualFolders", endpoint_virtual_folders),
    Route("/DisplayPreferences/{prefs_id}", endpoint_display_preferences),
    Route("/Shows/NextUp", endpoint_shows_nextup),
    Route("/Users/{user_id}/Items", endpoint_items),
    Route("/Users/{user_id}/Items/{item_id}", endpoint_item_details),
    Route("/Items", endpoint_items),
    Route("/Items/{item_id}/PlaybackInfo", endpoint_playback_info, methods=["GET", "POST"]),
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
    Route("/{path:path}", catch_all),
]

middleware = [
    Middleware(RequestLoggingMiddleware),
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
]

app = Starlette(debug=True, routes=routes, middleware=middleware)

# --- Main Execution ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    logger.info(f"--- Stash-Jellyfin Proxy v3.33 ---")
    logger.info(f"Binding: {PROXY_BIND}:{PROXY_PORT}")
    logger.info(f"Stash URL: {STASH_URL}")
    
    if check_stash_connection():
        logger.info("Starting Hypercorn server...")
        config = Config()
        config.bind = [f"{PROXY_BIND}:{PROXY_PORT}"]
        # Force hypercorn logs to console
        config.accesslog = logging.getLogger("hypercorn.access")
        config.access_log_format = "%(h)s %(l)s %(u)s %(t)s \"%(r)s\" %(s)s %(b)s"
        config.errorlog = logging.getLogger("hypercorn.error")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(serve(app, config))
        except KeyboardInterrupt:
            logger.info("Stopping...")
    else:
        logger.error("ABORTING: Could not connect to Stash. Check configuration.")
        sys.exit(1)
