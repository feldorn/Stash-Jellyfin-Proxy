# Stash-Jellyfin Proxy

A Python proxy server that enables Jellyfin-compatible media players (like Infuse) to connect to Stash media server by emulating the Jellyfin API.

## Current Version: v3.70

## User Preferences

Preferred communication style: Simple, everyday language.

## Project Status

**Phase 1 (Complete)**: Core proxy functionality
- Full Jellyfin API emulation for Infuse compatibility
- Stash GraphQL integration for all content types
- Saved filters support with complex transformations
- Configurable logging with file rotation
- Error resilience with retry logic

**Phase 2 (Complete)**: Web UI
- Embedded Web UI served from Python script on UI_PORT (8097)
- Dashboard with proxy status, Stash connection, active streams
- Configuration editor with all settings
- Log viewer with filtering and download

**Next Phases**:
- Phase 3: Docker containerization

## Core Features

### Jellyfin API Emulation
- Authentication (username/password)
- Library browsing (Scenes, Performers, Studios, Groups)
- Video streaming with subtitle support
- Image serving with caching
- 50+ Jellyfin endpoints for client compatibility

### Stash Integration
- GraphQL API queries for all content types
- Saved filter transformation (SCENES, PERFORMERS, STUDIOS, GROUPS)
- Pagination handling with offset-based slicing
- Tag-based library folders (TAG_GROUPS)
- Latest items for home screen (LATEST_GROUPS)

### Configuration (stash_jellyfin_proxy.conf)

| Setting | Description | Default |
|---------|-------------|---------|
| STASH_URL | Stash server URL | http://localhost:9999 |
| PROXY_BIND | Proxy bind address | 0.0.0.0 |
| PROXY_PORT | Proxy port | 8096 |
| UI_PORT | Web UI port (0 to disable) | 8097 |
| STASH_API_KEY | Stash API key | (required) |
| SJS_USER | Infuse login username | admin |
| SJS_PASSWORD | Infuse login password | (required) |
| TAG_GROUPS | Comma-separated tag names for library folders | (empty) |
| LATEST_GROUPS | Libraries to show on home screen | Scenes |
| SERVER_NAME | Server name shown in clients | Stash Media Server |
| STASH_TIMEOUT | API request timeout (seconds) | 30 |
| STASH_RETRIES | API retry count | 3 |
| LOG_DIR | Log file directory | . |
| LOG_FILE | Log file name | stash_jellyfin_proxy.log |
| LOG_LEVEL | Logging level (DEBUG/INFO/WARNING/ERROR) | INFO |
| LOG_MAX_SIZE_MB | Max log file size before rotation | 10 |
| LOG_BACKUP_COUNT | Number of backup log files | 3 |

### Command Line Options

```bash
./stash_jellyfin_proxy.py [--debug] [--no-log-file] [--no-ui]
```

- `--debug`: Enable debug logging (overrides LOG_LEVEL)
- `--no-log-file`: Disable file logging (console only)
- `--no-ui`: Disable Web UI server

## Technical Architecture

### Python Proxy Server (stash_jellyfin_proxy.py)

- **Framework**: Starlette (ASGI) with Hypercorn server
- **Port**: 8096 (default Jellyfin port)
- **Authentication**: Simple shared credentials
- **Logging**: Console + rotating file handler

### Key Components

1. **Jellyfin Endpoints**: 50+ endpoints for full client compatibility
2. **Stash GraphQL Client**: Handles all Stash API communication with retry logic
3. **Filter Transformer**: Converts saved filters to GraphQL query format
4. **Image Handler**: Serves and caches images with optional resizing
5. **Stream Proxy**: Redirects video streams to Stash

### Dependencies

- Python 3.8+
- hypercorn (ASGI server)
- starlette (web framework)
- requests (HTTP client)
- Pillow (optional, for image resizing)

## Files

| File | Description |
|------|-------------|
| stash_jellyfin_proxy.py | Main proxy server (v3.68) |
| stash_jellyfin_proxy.conf | Configuration file |

## Recent Changes

- v3.70: Config save now preserves comments and formatting (updates values in-place); fixed socket.send() errors by suppressing asyncio logger; v3.69 middleware improvements included
- v3.69: Fixed streaming disconnect errors - replaced BaseHTTPMiddleware with pure ASGI middleware to suppress expected client disconnect errors during video seeking; dashboard logs now show last 10 complete entries
- v3.68: Fixed video streaming - now uses true chunked streaming instead of buffering entire file, eliminating 20+ second delays on large videos
- v3.67: Enhanced stream tracking - Dashboard now shows start time, user, client IP, and client type for active streams; graceful error on port-in-use
- v3.66: Fixed person-performer-* ID parsing for Infuse requests, added null checking for performer lookups, fixed Ctrl-C graceful shutdown with proper signal handling
- v3.65: Embedded Web UI in Python script - Dashboard, Configuration editor, Log viewer all served on UI_PORT (8097)
- v3.64: Implemented Infuse search functionality - now queries Stash with searchTerm parameter using relevance sorting
- v3.62: Stream logging now shows video title (or filename), fixed duplicate "started" messages by tracking active streams
- v3.61: Added "Stream stopped" logging, fixed false resume detection (threshold now 90s to match Infuse buffering)
- v3.60: Added stream resume detection - logs when video resumes after pause (10+ seconds of inactivity)
- v3.59: Descriptive stream logging - shows "Stream started: scene-12345" for new streams, range requests go to DEBUG
- v3.58: Smarter logging - only important events at INFO level (auth, streams, errors, slow requests)
- v3.57: Improved request logging - cleaner single-line format showing path -> status (time)
- v3.56: Added MediaSegments endpoint (stub) - Infuse doesn't support this API yet
- v3.55: Cleaned up logging - moved verbose messages from INFO to DEBUG level
- v3.54: Made SERVER_ID a required config value (app stops if not set)
- v3.53: Fixed log level not being properly applied to all handlers
- v3.52: Added file logging with rotation, debug flag improvements
- v3.51: Fixed SERVER_ID consistency for Infuse pairing
- v3.50: Added 20+ Jellyfin endpoints, enhanced filter transformation, externalized config, retry logic