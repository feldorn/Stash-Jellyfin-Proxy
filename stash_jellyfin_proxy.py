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
from typing import Optional, List, Dict, Any
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
SERVER_ID = "stash-proxy-v1"
ACCESS_TOKEN = str(uuid.uuid4())

def format_jellyfin_item(scene: Dict[str, Any]) -> Dict[str, Any]:
    item_id = scene.get("id")
    title = scene.get("title") or scene.get("code") or f"Scene {item_id}"
    date = scene.get("date")
    files = scene.get("files", [])
    path = files[0].get("path") if files else ""
    studio = scene.get("studio", {}).get("name") if scene.get("studio") else None
    tags = [t.get("name") for t in scene.get("tags", [])]
    
    people = []
    for p in scene.get("performers", []):
        people.append({
            "Name": p.get("name"),
            "Id": p.get("id"),
            "Type": "Actor",
            "Role": "Performer"
        })

    return {
        "Name": title,
        "Id": item_id,
        "ServerId": SERVER_ID,
        "Type": "Movie",
        "MediaType": "Video",
        "ProductionYear": int(date[:4]) if date else None,
        "PremiereDate": f"{date}T00:00:00.0000000Z" if date else None,
        "DateCreated": f"{date}T00:00:00.0000000Z" if date else None,
        "Path": path,
        "Studios": [{"Name": studio}] if studio else [],
        "Genres": tags,
        "Tags": tags,
        "People": people,
        "ImageTags": {"Primary": item_id, "Thumb": item_id},
        "Container": "mp4",
        "SupportsSync": True,
        "RunTimeTicks": int(files[0].get("duration", 0) * 10000000) if files else 0
    }

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

async def endpoint_user_views(request):
    return JSONResponse({
        "Items": [
            {
                "Name": "All Scenes",
                "Id": "root-scenes",
                "ServerId": SERVER_ID,
                "Type": "CollectionFolder",
                "CollectionType": "movies"
            },
            {
                "Name": "Studios",
                "Id": "root-studios",
                "ServerId": SERVER_ID,
                "Type": "CollectionFolder",
                "CollectionType": "movies"
            }
        ],
        "TotalRecordCount": 2
    })

async def endpoint_grouping_options(request):
    # Infuse requests this and if it 404s, it shows "an error occurred"
    return JSONResponse([])

async def endpoint_items(request):
    user_id = request.path_params.get("user_id")
    parent_id = request.query_params.get("ParentId")
    ids = request.query_params.get("Ids")
    
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
        q = """query FindScenes { findScenes(scene_filter: {sort: date, direction: DESC}, filter: {per_page: 50}) { scenes { id title code date files { path duration } studio { name } tags { name } performers { name id } } } }"""
        res = stash_query(q)
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s))

    elif parent_id == "root-studios":
        q = """query FindStudios { findStudios(filter: {per_page: 50, sort: name, direction: ASC}) { studios { id name } } }"""
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
        q = """query FindScenes($sid: ID!) { findScenes(scene_filter: {studios: {value: $sid, modifier: EQUALS}, sort: date, direction: DESC}, filter: {per_page: 50}) { scenes { id title code date files { path duration } studio { name } tags { name } performers { name id } } } }"""
        res = stash_query(q, {"sid": studio_id})
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s))
            
    return JSONResponse({"Items": items, "TotalRecordCount": len(items)})

async def endpoint_item_details(request):
    item_id = request.path_params.get("item_id")
    q = """query FindScene($id: ID!) { findScene(id: $id) { id title code date files { path duration } studio { name } tags { name } performers { name id } } }"""
    res = stash_query(q, {"id": item_id})
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

async def endpoint_stream(request):
    item_id = request.path_params.get("item_id")
    stash_stream_url = f"{STASH_URL}/scene/{item_id}/stream"
    logger.info(f"Redirecting stream for {item_id} to {stash_stream_url}")
    return RedirectResponse(url=stash_stream_url)

async def endpoint_image(request):
    item_id = request.path_params.get("item_id")
    stash_img_url = f"{STASH_URL}/scene/{item_id}/screenshot"
    return RedirectResponse(url=stash_img_url)

# --- App Construction ---
routes = [
    Route("/", endpoint_root),
    Route("/System/Info", endpoint_system_info),
    Route("/System/Info/Public", endpoint_public_info),
    Route("/Users/AuthenticateByName", endpoint_authenticate_by_name, methods=["POST"]),
    Route("/Users/{user_id}/Views", endpoint_user_views),
    Route("/Users/{user_id}/GroupingOptions", endpoint_grouping_options),
    Route("/Users/{user_id}/Items", endpoint_items),
    Route("/Users/{user_id}/Items/{item_id}", endpoint_item_details),
    Route("/Items", endpoint_items),
    Route("/Videos/{item_id}/stream", endpoint_stream),
    Route("/Videos/{item_id}/stream.mp4", endpoint_stream),
    Route("/Items/{item_id}/Images/Primary", endpoint_image),
    Route("/Items/{item_id}/Images/Thumb", endpoint_image),
    Route("/PlaybackInfo", endpoint_playback_info, methods=["POST", "GET"]),
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
    
    logger.info(f"--- Stash-Jellyfin Proxy v1.2 ---")
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
