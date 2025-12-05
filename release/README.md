# Stash-Jellyfin Proxy

**Version 5.00**

A Python proxy server that enables Jellyfin-compatible media players (like Infuse) to connect to Stash by emulating the Jellyfin API.

## Features

- **Jellyfin API Emulation**: Implements 50+ Jellyfin endpoints for broad client compatibility
- **Full Stash Integration**: Scenes, Performers, Studios, Groups, and Tags
- **Tag-Based Libraries**: Create custom library folders based on Stash tags
- **Saved Filters Support**: Browse your Stash saved filters as folders
- **Web Configuration UI**: Dashboard with status, active streams, statistics, and settings
- **Docker Support**: Ready-to-use Docker container with PUID/PGID support
- **IP Security**: Auto-banning for failed authentication attempts

## Quick Start

### Standalone

1. Install dependencies:
   ```bash
   pip install hypercorn starlette requests Pillow
   ```

2. Configure `stash_jellyfin_proxy.conf` with your Stash URL and API key

3. Run:
   ```bash
   python stash_jellyfin_proxy.py
   ```

4. Open Web UI at `http://localhost:8097`

5. Add server in Infuse: `http://your-server:8096`

### Docker

```bash
docker run -d \
  --name stash-jellyfin-proxy \
  -p 8096:8096 \
  -p 8097:8097 \
  -v /path/to/config:/config \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=America/New_York \
  stash-jellyfin-proxy:latest
```

## Configuration

Edit `stash_jellyfin_proxy.conf`:

| Setting | Description | Default |
|---------|-------------|---------|
| `STASH_URL` | Your Stash server URL | `http://localhost:9999` |
| `STASH_API_KEY` | API key from Stash Settings > Security | Required |
| `SJS_USER` | Username for Infuse login | Required |
| `SJS_PASSWORD` | Password for Infuse login | Required |
| `TAG_GROUPS` | Comma-separated tags to show as library folders | Empty |
| `PROXY_PORT` | Jellyfin API port | `8096` |
| `UI_PORT` | Web UI port (0 to disable) | `8097` |

See the config file for all available options.

## Connecting from Infuse

1. Add a new share in Infuse
2. Select "Jellyfin" as the server type
3. Enter your proxy server address (e.g., `http://192.168.1.100:8096`)
4. Use the `SJS_USER` and `SJS_PASSWORD` credentials you configured

## Requirements

- Python 3.8+
- Stash media server with API access enabled
- Dependencies: `hypercorn`, `starlette`, `requests`
- Optional: `Pillow` for image resizing

## Architecture

```
Infuse/Jellyfin Client
        |
        v
  Stash-Jellyfin Proxy (port 8096)
        |
        v
  Stash GraphQL API (port 9999)
```

The proxy translates Jellyfin API requests into Stash GraphQL queries, handles authentication, serves images, and proxies video streams.

## Web UI

Access the configuration dashboard at `http://your-server:8097`:

- **Dashboard**: Proxy status, Stash connection, active streams, usage statistics
- **Configuration**: All settings with live updates
- **Logs**: Filterable log viewer with download

## Known Limitations

- Single-user authentication (one set of credentials)
- Infuse caches images aggressively; clear metadata cache if artwork doesn't update
- Dashboard may briefly pause during stream initialization

## License

MIT License - Free to use and modify.
