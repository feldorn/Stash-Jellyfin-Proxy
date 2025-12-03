"Id": f"performer-{p['id']}",
                "ServerId": SERVER_ID,
                "Type": "Person",
                "PrimaryImageTag": "img" if p.get("image_path") else None
            })
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting persons: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_studios(request):
    """Return studios list via /Studios endpoint."""
    start_index = int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0)
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 50)
    
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
            items.append({
                "Name": s["name"],
                "Id": f"studio-{s['id']}",
                "ServerId": SERVER_ID,
                "Type": "Studio",
                "PrimaryImageTag": "img" if s.get("image_path") else None
            })
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
    Middleware(RequestLoggingMiddleware),
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
]

app = Starlette(debug=True, routes=routes, middleware=middleware)

# --- Web UI Server ---
PROXY_RUNNING = False  # Track if proxy is running

async def ui_index(request):
    """Serve the Web UI."""
    return Response(WEB_UI_HTML, media_type="text/html")

async def ui_api_status(request):
    """Return proxy status."""
    return JSONResponse({
        "running": PROXY_RUNNING,
        "version": "v3.65",
        "proxyBind": PROXY_BIND,
        "proxyPort": PROXY_PORT,
        "stashConnected": STASH_CONNECTED,
        "stashVersion": STASH_VERSION,
        "stashUrl": STASH_URL
    })

async def ui_api_config(request):
    """Get or set configuration."""
    if request.method == "GET":
        return JSONResponse({
            "STASH_URL": STASH_URL,
            "STASH_API_KEY": "*" * min(len(STASH_API_KEY), 20) if STASH_API_KEY else "",
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
            "LOG_LEVEL": LOG_LEVEL,
            "LOG_DIR": LOG_DIR,
            "LOG_FILE": LOG_FILE,
            "LOG_MAX_SIZE_MB": LOG_MAX_SIZE_MB,
            "LOG_BACKUP_COUNT": LOG_BACKUP_COUNT
        })
    elif request.method == "POST":
        try:
            data = await request.json()
            config_lines = []
            config_keys = [
                "STASH_URL", "STASH_API_KEY", "PROXY_BIND", "PROXY_PORT", "UI_PORT",
                "SJS_USER", "SJS_PASSWORD", "SERVER_ID", "SERVER_NAME",
                "TAG_GROUPS", "LATEST_GROUPS", "STASH_TIMEOUT", "STASH_RETRIES",
                "LOG_LEVEL", "LOG_DIR", "LOG_FILE", "LOG_MAX_SIZE_MB", "LOG_BACKUP_COUNT"
            ]
            
            # Read existing config to preserve values not being updated
            existing_config = {}
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, _, value = line.partition('=')
                            existing_config[key.strip()] = value.strip().strip('"').strip("'")
            
            # Update with new values
            for key in config_keys:
                if key in data:
                    value = data[key]
                    # Don't update masked passwords
                    if key in ["STASH_API_KEY", "SJS_PASSWORD"] and value.startswith("*"):
                        value = existing_config.get(key, "")
                    if isinstance(value, list):
                        value = ", ".join(value)
                    existing_config[key] = str(value)
            
            # Write config file
            with open(CONFIG_FILE, 'w') as f:
                f.write("# Stash-Jellyfin Proxy Configuration\n")
                f.write("# Generated by Web UI\n\n")
                for key in config_keys:
                    if key in existing_config:
                        f.write(f'{key} = "{existing_config[key]}"\n')
            
            return JSONResponse({"success": True})
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
    for scene_id, info in _active_streams.items():
        streams.append({
            "id": scene_id,
            "title": info.get("title", scene_id),
            "lastSeen": info.get("last_seen", 0)
        })
    return JSONResponse({"streams": streams})

ui_routes = [
    Route("/", ui_index),
    Route("/api/status", ui_api_status),
    Route("/api/config", ui_api_config, methods=["GET", "POST"]),
    Route("/api/logs", ui_api_logs),
    Route("/api/streams", ui_api_streams),
]

ui_middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
]

ui_app = Starlette(debug=False, routes=ui_routes, middleware=ui_middleware)

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
    
    logger.info(f"--- Stash-Jellyfin Proxy v3.65 ---")
    logger.info(f"Binding: {PROXY_BIND}:{PROXY_PORT}")
    logger.info(f"Stash URL: {STASH_URL}")
    
    if check_stash_connection():
        PROXY_RUNNING = True
        
        # Configure proxy server
        proxy_config = Config()
        proxy_config.bind = [f"{PROXY_BIND}:{PROXY_PORT}"]
        proxy_config.accesslog = logging.getLogger("hypercorn.access")
        proxy_config.access_log_format = "%(h)s %(l)s %(u)s %(t)s \"%(r)s\" %(s)s %(b)s"
        proxy_config.errorlog = logging.getLogger("hypercorn.error")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run_servers():
            """Run both proxy and UI servers."""
            tasks = [serve(app, proxy_config)]
            
            # Start UI server if enabled
            if UI_PORT > 0 and not args.no_ui:
                ui_config = Config()
                ui_config.bind = [f"{PROXY_BIND}:{UI_PORT}"]
                ui_config.accesslog = None  # Disable access logging for UI
                ui_config.errorlog = logging.getLogger("hypercorn.error")
                tasks.append(serve(ui_app, ui_config))
                logger.info(f"Web UI: http://{PROXY_BIND}:{UI_PORT}")
            
            logger.info("Starting Hypercorn server...")
            await asyncio.gather(*tasks)
        
        try:
            loop.run_until_complete(run_servers())
        except KeyboardInterrupt:
            logger.info("Stopping...")
    else:
        logger.error("ABORTING: Could not connect to Stash. Check configuration.")
        sys.exit(1)
