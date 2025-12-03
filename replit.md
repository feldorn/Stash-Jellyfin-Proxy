# Stash-Jellyfin Proxy

A Python proxy server that enables Jellyfin-compatible media players (like Infuse) to connect to Stash media server by emulating the Jellyfin API.

## Current Version: v3.54

## User Preferences

Preferred communication style: Simple, everyday language.

## Project Status

**Phase 1 (Complete)**: Core proxy functionality
- Full Jellyfin API emulation for Infuse compatibility
- Stash GraphQL integration for all content types
- Saved filters support with complex transformations
- Configurable logging with file rotation
- Error resilience with retry logic

**Next Phases**:
- Phase 2: Web UI for configuration and monitoring
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
./stash_jellyfin_proxy.py [--debug] [--no-log-file]
```

- `--debug`: Enable debug logging (overrides LOG_LEVEL)
- `--no-log-file`: Disable file logging (console only)

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
| stash_jellyfin_proxy.py | Main proxy server (v3.54) |
| stash_jellyfin_proxy.conf | Configuration file |

## Recent Changes

- v3.54: Made SERVER_ID a required config value (app stops if not set)
- v3.53: Fixed log level not being properly applied to all handlers
- v3.52: Added file logging with rotation, debug flag improvements
- v3.51: Fixed SERVER_ID consistency for Infuse pairing
- v3.50: Added 20+ Jellyfin endpoints, enhanced filter transformation, externalized config, retry logic