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
STASH_API_KEY = ""
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
SJS_USER_PASSWORD = "infuse12345"
SJS_USER_ID = "user-1"

# Load Config
if os.path.isfile(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, 'r') as f:
            exec(f.read())
    except Exception as e:
        print(f"Error loading config file {CONFIG_FILE}: {e}", file=sys.stderr)
        sys.exit(1)
else:
    print(f"Warning: Config file {CONFIG_FILE} not found. Using defaults/env vars.")
    STASH_URL = os.getenv("STASH_URL", STASH_URL)
    STASH_API_KEY = os.getenv("STASH_API_KEY", STASH_API_KEY)
    SJS_USER_PASSWORD = os.getenv("SJS_USER_PASSWORD", SJS_USER_PASSWORD)
    SJS_USER_ID = os.getenv("SJS_USER_ID", SJS_USER_ID)

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
STASH_HEADERS = {
    "ApiKey": STASH_API_KEY,
    "Content-Type": "application/json"
}

def check_stash_connection():
    """Verify we can talk to Stash at startup."""
    try:
        logger.info(f"Testing connection to Stash at {GRAPHQL_URL}...")
        resp = requests.post(
            GRAPHQL_URL, 
            json={"query": "{ version { version } }"}, 
            headers=STASH_HEADERS, 
            timeout=5
        )
        resp.raise_for_status()
        v = resp.json().get("data", {}).get("version", {}).get("version", "unknown")
        logger.info(f"✅ Connected to Stash! Version: {v}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to connect to Stash: {e}")
        logger.error("Please check STASH_URL and STASH_API_KEY in your config.")
        return False

def stash_query(query: str, variables: Dict[str, Any] = None) -> Dict[str, Any]:
    try:
        resp = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables or {}}, headers=STASH_HEADERS, timeout=10)
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
    
    if studio:
        item["Overview"] = f"Scene from {studio}"
    
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
    
    # Accept config key OR simple "password" string if user puts it there
    if pw == SJS_USER_PASSWORD:
        logger.info("Auth SUCCESS")
        return JSONResponse({
            "User": {
                "Name": username,
                "Id": SJS_USER_ID,
                "Policy": {"IsAdministrator": True}
            },
            "SessionInfo": {
                "UserId": SJS_USER_ID,
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
        "Id": SJS_USER_ID,
        "HasPassword": True,
        "Policy": {"IsAdministrator": True, "EnableContentDeletion": False}
    }])

async def endpoint_user_by_id(request):
    # Return user profile
    return JSONResponse({
        "Name": "Stash User",
        "Id": SJS_USER_ID,
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
                "Name": "All Scenes",
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
            }
        ],
        "TotalRecordCount": 2
    })

async def endpoint_grouping_options(request):
    # Infuse requests this and if it 404s, it shows "an error occurred"
    return JSONResponse([])

async def endpoint_virtual_folders(request):
    # Infuse requests library virtual folders
    return JSONResponse([
        {
            "Name": "All Scenes",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-scenes"
        },
        {
            "Name": "Studios",
            "Locations": [],
            "CollectionType": "movies",
            "ItemId": "root-studios"
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

async def endpoint_items(request):
    user_id = request.path_params.get("user_id")
    # Handle both ParentId and parentId (Infuse uses lowercase)
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    ids = request.query_params.get("Ids") or request.query_params.get("ids")
    
    # Debug: Log all query params
    logger.debug(f"Items endpoint - ParentId: {parent_id}, Ids: {ids}, All params: {dict(request.query_params)}")
    
    items = []
    
    if ids:
        # Specific items requested
        id_list = ids.split(',')
        for iid in id_list:
            q = """query FindScene($id: ID!) { findScene(id: $id) { id title code date files { path duration } studio { name } tags { name } performers { name id } } }"""
            res = stash_query(q, {"id": iid})
            scene = res.get("data", {}).get("findScene")
            if scene:
                items.append(format_jellyfin_item(scene))
    
    elif parent_id == "root-scenes":
        q = """query FindScenes { findScenes(filter: {per_page: 50, sort: "date", direction: DESC}) { scenes { id title code date files { path duration } studio { name } } } }"""
        res = stash_query(q)
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s, parent_id="root-scenes"))

    elif parent_id == "root-studios":
        q = """query FindStudios { findStudios(filter: {per_page: 50, sort: "name", direction: ASC}) { studios { id name } } }"""
        res = stash_query(q)
        for s in res.get("data", {}).get("findStudios", {}).get("studios", []):
            items.append({
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": SERVER_ID,
                "Type": "Folder",
                "ImageTags": {}
            })
            
    elif parent_id and parent_id.startswith("studio-"):
        studio_id = parent_id.replace("studio-", "")
        # Simplified query without modifier - just filter by studio value
        q = """query FindScenes($sid: [ID!]) { findScenes(scene_filter: {studios: {value: $sid}}, filter: {per_page: 50, sort: "date", direction: DESC}) { scenes { id title code date files { path duration } studio { name } } } }"""
        res = stash_query(q, {"sid": [studio_id]})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Studio {studio_id} returned {len(scenes)} scenes")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))
            
    return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})

async def endpoint_item_details(request):
    item_id = request.path_params.get("item_id")
    
    # Handle special folder IDs - return the folder ITSELF (not children)
    if item_id == "root-scenes":
        # Return folder metadata, not children (children come from /Items?parentId=root-scenes)
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
            "ChildCount": 50,
            "RecursiveItemCount": 50,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-scenes"}
        })
    
    elif item_id == "root-studios":
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
            "ChildCount": 50,
            "RecursiveItemCount": 50,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-studios"}
        })
    
    elif item_id.startswith("studio-"):
        # Return studio folder metadata
        studio_id = item_id.replace("studio-", "")
        return JSONResponse({
            "Name": f"Studio {studio_id}",
            "SortName": f"Studio {studio_id}",
            "Id": item_id,
            "ServerId": SERVER_ID,
            "Type": "Folder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
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
    
    q = """query FindScene($id: ID!) { findScene(id: $id) { id title code date files { path duration } studio { name } } }"""
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
    Fetch content from Stash using requests library for proper redirect handling.
    Returns (data, content_type, response_headers).
    """
    try:
        import requests
    except ImportError:
        logger.error("requests library not installed. Run: pip install requests")
        raise Exception("requests library required for media proxy")
    
    # Create session with persistent headers
    session = requests.Session()
    if STASH_API_KEY:
        session.headers["ApiKey"] = STASH_API_KEY
    
    # Add extra headers
    headers = extra_headers or {}
    
    try:
        response = session.get(url, headers=headers, timeout=timeout, stream=stream, allow_redirects=True)
        
        # Log response details for debugging
        content_type = response.headers.get('Content-Type', 'application/octet-stream')
        logger.debug(f"Fetch {url}: status={response.status_code}, type={content_type}, size={len(response.content) if not stream else 'streaming'}")
        
        # Check if we got HTML instead of media (indicates auth failure)
        if 'text/html' in content_type:
            logger.error(f"Got HTML response instead of media. First 200 chars: {response.text[:200]}")
            raise Exception(f"Authentication failed - received HTML instead of media")
        
        response.raise_for_status()
        
        # Build response headers dict
        resp_headers = dict(response.headers)
        
        if stream:
            # For streaming, return chunks
            data = b''.join(response.iter_content(chunk_size=65536))
        else:
            data = response.content
        
        logger.debug(f"Fetch success: {len(data)} bytes, type={content_type}")
        return data, content_type, resp_headers
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
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
    """Proxy image from Stash with proper authentication."""
    item_id = request.path_params.get("item_id")
    numeric_id = get_numeric_id(item_id)
    stash_img_url = f"{STASH_URL}/scene/{numeric_id}/screenshot"
    
    logger.info(f"Proxying image for {item_id} from {stash_img_url}")
    
    try:
        data, content_type, _ = fetch_from_stash(stash_img_url, timeout=30)
        
        from starlette.responses import Response
        logger.info(f"Image response: {len(data)} bytes, type={content_type}")
        return Response(content=data, media_type=content_type)
        
    except Exception as e:
        logger.error(f"Image proxy error: {e}")
        from starlette.responses import Response
        return Response(content=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82', media_type='image/png')

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
    
    logger.info(f"--- Stash-Jellyfin Proxy v3.1 ---")
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
