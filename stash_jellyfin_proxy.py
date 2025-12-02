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

# --- Configuration Loading ---
CONFIG_FILE = "/home/chris/.scripts.conf"

# Default Configuration
STASH_URL = "https://stash.feldorn.com"
STASH_API_KEY = ""  # Real Stash API key from Settings -> Security -> API Key
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
# User credentials for Infuse authentication (matches .scripts.conf variable names)
SJS_USER = "chris"
SJS_PASSWORD = "infuse12345"

# Load Config - parses .scripts.conf which uses KEY="value" format
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
    SJS_USER = _config.get("SJS_USER", SJS_USER)
    SJS_PASSWORD = _config.get("SJS_PASSWORD", SJS_PASSWORD)
    print(f"Loaded config from {CONFIG_FILE}: user={SJS_USER}")
else:
    print(f"Warning: Config file {CONFIG_FILE} not found or empty. Using defaults/env vars.")
    STASH_URL = os.getenv("STASH_URL", STASH_URL)
    STASH_API_KEY = os.getenv("STASH_API_KEY", STASH_API_KEY)
    SJS_USER = os.getenv("SJS_USER", SJS_USER)
    SJS_PASSWORD = os.getenv("SJS_PASSWORD", SJS_PASSWORD)

# Session management for cookie-based auth
STASH_SESSION = None  # Will hold requests.Session with auth cookies

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
    
    # Use STASH_API_KEY if set, otherwise use SJS_PASSWORD
    api_key = STASH_API_KEY if STASH_API_KEY else SJS_PASSWORD
    if api_key:
        STASH_SESSION.headers["ApiKey"] = api_key
        logger.info(f"Session configured with ApiKey header")
    
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

def stash_query(query: str, variables: Dict[str, Any] = None) -> Dict[str, Any]:
    try:
        session = get_stash_session()
        resp = session.post(GRAPHQL_URL, json={"query": query, "variables": variables or {}}, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Stash API Query Error: {e}")
        return {"errors": [str(e)]}

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
        item["MediaSources"] = [{
            "Id": item_id,
            "Path": path,
            "Protocol": "Http",
            "Type": "Default",
            "Container": "mp4",
            "Name": title,
            "SupportsDirectPlay": True,
            "SupportsDirectStream": True,
            "SupportsTranscoding": False
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
    return JSONResponse({
        "Items": [
            {
                "Name": "Scenes",
                "Id": "root-scenes",
                "ServerId": SERVER_ID,
                "Type": "CollectionFolder",
                "CollectionType": "movies",
                "IsFolder": True,
                "ImageTags": {},
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
                "ImageTags": {},
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
                "ImageTags": {},
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
                "ImageTags": {},
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-groups"}
            }
        ],
        "TotalRecordCount": 4
    })

async def endpoint_grouping_options(request):
    # Infuse requests this and if it 404s, it shows "an error occurred"
    return JSONResponse([])

async def endpoint_virtual_folders(request):
    # Infuse requests library virtual folders
    return JSONResponse([
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
    ])

async def endpoint_shows_nextup(request):
    # Infuse requests next up episodes - return empty
    return JSONResponse({"Items": [], "TotalRecordCount": 0})

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
    sort_by = request.query_params.get("SortBy") or request.query_params.get("sortBy") or "PremiereDate"
    sort_order = request.query_params.get("SortOrder") or request.query_params.get("sortOrder") or "Descending"
    
    # Map Jellyfin sort fields to Stash
    sort_mapping = {
        "SortName": "title",
        "Name": "title",
        "PremiereDate": "date",
        "DateCreated": "date",
        "DatePlayed": "date",
        "ProductionYear": "date",
        "Random": "random",
        "Runtime": "duration",
        "CommunityRating": "rating",
        "PlayCount": "play_count",
    }
    
    stash_sort = sort_mapping.get(sort_by, "date")
    stash_direction = "ASC" if sort_order == "Ascending" else "DESC"
    
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
    
    # Full scene fields for queries (include performer image_path for People images)
    scene_fields = "id title code date details files { path duration } studio { name } tags { name } performers { name id image_path }"
    
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
    
    elif parent_id == "root-scenes":
        # Calculate page number from startIndex (Stash uses 1-indexed pages)
        page = (start_index // limit) + 1
        
        # First get total count
        count_q = """query { findScenes { count } }"""
        count_res = stash_query(count_q)
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
        
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
        total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)
        
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
        total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
        
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
        total_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)
        
        # Calculate page
        page = (start_index // limit) + 1
        
        q = """query FindMovies($page: Int!, $per_page: Int!) { 
            findMovies(filter: {page: $page, per_page: $per_page, sort: "name", direction: ASC}) { 
                movies { id name front_image_path scene_count } 
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
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"group-{m['id']}"}
            }
            if m.get("front_image_path"):
                group_item["ImageTags"] = {"Primary": "img"}
            else:
                group_item["ImageTags"] = {}
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
            
    return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})

async def endpoint_item_details(request):
    item_id = request.path_params.get("item_id")
    
    # Full scene fields for queries (include performer image_path for People images)
    scene_fields = "id title code date details files { path duration } studio { name } tags { name } performers { name id image_path }"
    
    # Handle special folder IDs - return the folder ITSELF (not children)
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

async def endpoint_playback_info(request):
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

async def endpoint_image(request):
    """Proxy image from Stash with proper authentication. Handles scenes, studios, performers, and groups."""
    item_id = request.path_params.get("item_id")
    
    # Determine image URL based on item type
    if item_id.startswith("studio-"):
        numeric_id = item_id.replace("studio-", "")
        stash_img_url = f"{STASH_URL}/studio/{numeric_id}/image"
    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        if item_id.startswith("performer-"):
            numeric_id = item_id.replace("performer-", "")
        else:
            numeric_id = item_id.replace("person-", "")
        stash_img_url = f"{STASH_URL}/performer/{numeric_id}/image"
    elif item_id.startswith("group-"):
        numeric_id = item_id.replace("group-", "")
        stash_img_url = f"{STASH_URL}/movie/{numeric_id}/front_image"
    elif item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
        stash_img_url = f"{STASH_URL}/scene/{numeric_id}/screenshot"
    else:
        # Fallback - try as scene
        numeric_id = get_numeric_id(item_id)
        stash_img_url = f"{STASH_URL}/scene/{numeric_id}/screenshot"
    
    logger.info(f"Proxying image for {item_id} from {stash_img_url}")
    
    # Cache control headers to help with Infuse caching issues
    cache_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    
    try:
        data, content_type, _ = fetch_from_stash(stash_img_url, timeout=30)
        
        from starlette.responses import Response
        logger.info(f"Image response: {len(data)} bytes, type={content_type}")
        return Response(content=data, media_type=content_type, headers=cache_headers)
        
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        from starlette.responses import Response
        # Return transparent 1x1 PNG as fallback
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
    Route("/Users/{user_id}/GroupingOptions", endpoint_grouping_options),
    Route("/Library/VirtualFolders", endpoint_virtual_folders),
    Route("/DisplayPreferences/{prefs_id}", endpoint_display_preferences),
    Route("/Shows/NextUp", endpoint_shows_nextup),
    Route("/Users/{user_id}/Items", endpoint_items),
    Route("/Users/{user_id}/Items/{item_id}", endpoint_item_details),
    Route("/Items", endpoint_items),
    Route("/Videos/{item_id}/stream", endpoint_stream),
    Route("/Videos/{item_id}/stream.mp4", endpoint_stream),
    Route("/Items/{item_id}/Images/Primary", endpoint_image),
    Route("/Items/{item_id}/Images/Thumb", endpoint_image),
    Route("/PlaybackInfo", endpoint_playback_info, methods=["POST", "GET"]),
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
    
    logger.info(f"--- Stash-Jellyfin Proxy v3.7 ---")
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
