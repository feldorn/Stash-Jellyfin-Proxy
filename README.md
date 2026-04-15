# Stash-Jellyfin Proxy

**Version 6.01**

A single-file Python proxy server that enables Jellyfin-compatible media players to connect to [Stash](https://stashapp.cc/) by emulating the Jellyfin API.

## Supported Clients

| Client | Platform | Status |
|--------|----------|--------|
| **Infuse** | iOS, tvOS, macOS | Fully supported |
| **Swiftfin** | iOS, tvOS | Partial support |
| **SenPlayer** | iOS | Fully supported |
| **Jellyfin Android** | Android | Partial support |
| **Findroid** | Android | Partial support |
| Other Jellyfin clients | Various | Should work (untested) |

## Features

- **Jellyfin API Emulation**: Implements 50+ Jellyfin endpoints for broad client compatibility
- **Multi-Client Support**: Tested with Infuse, Swiftfin, and SenPlayer
- **Full Stash Integration**: Scenes, Performers, Studios, Groups, and Tags
- **Play Tracking**: Automatic watched/resume sync with Stash (>90% watched marks played, otherwise saves resume position)
- **Favorites**: Tag-based scene and group favorites, native performer favorites, studio favorites — all synced back to Stash
- **Tag-Based Libraries**: Create custom library folders based on Stash tags
- **Saved Filters Support**: Browse your Stash saved filters as folders
- **Subtitle Support**: SRT and VTT subtitle delivery from Stash captions
- **Rich Metadata**: Codec details, resolution, bitrate, frame rate, and channel layout reported to clients
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

5. Add server in your Jellyfin client: `http://your-server:8096`

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
| `SJS_USER` | Username for client login | Required |
| `SJS_PASSWORD` | Password for client login | Required |
| `TAG_GROUPS` | Comma-separated tags to show as library folders | Empty |
| `FAVORITE_TAG` | Tag name used for scene and group favorites (e.g., `Favorite`) | Empty (disabled) |
| `ENABLE_ALL_TAGS` | Show "All Tags" subfolder in Tags library | `false` |
| `PROXY_PORT` | Jellyfin API port | `8096` |
| `UI_PORT` | Web UI port (0 to disable) | `8097` |

See the config file for all available options. Settings can also be changed via the Web UI or environment variables.

## Connecting Clients

### Infuse (iOS / tvOS / macOS)

1. Add a new share in Infuse
2. Select "Jellyfin" as the server type
3. Enter your proxy server address (e.g., `http://192.168.1.100:8096`)
4. Use the `SJS_USER` and `SJS_PASSWORD` credentials you configured

### Swiftfin (iOS / tvOS)

1. Add a new server
2. Enter your proxy server address (e.g., `http://192.168.1.100:8096`)
3. Log in with your `SJS_USER` and `SJS_PASSWORD` credentials

### SenPlayer (iOS)

1. Add a Jellyfin/Emby server
2. Enter your proxy server address
3. Log in with your configured credentials

## Favorites

Favorites work differently depending on the item type:

- **Scenes**: Requires `FAVORITE_TAG` to be set in config (e.g., `Favorite`). Toggling a scene's favorite adds/removes this tag. The tag is auto-created in Stash if it doesn't exist.
- **Groups**: Uses the same `FAVORITE_TAG` approach as scenes. Toggling a group's favorite adds/removes the tag via `movieUpdate`. Requires `FAVORITE_TAG` to be configured.
- **Performers**: Uses Stash's native `favorite` boolean field. No configuration needed.
- **Studios**: Uses the `studioUpdate` mutation. No configuration needed.

## Requirements

- Python 3.8+
- Stash media server with API access enabled
- Dependencies: `hypercorn`, `starlette`, `requests`
- Optional: `Pillow` for image resizing

## Architecture

```
Jellyfin Client (Infuse / Swiftfin / SenPlayer)
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
- Clients cache images aggressively; clear metadata cache if artwork doesn't update
- Scene and group favorites require `FAVORITE_TAG` to be configured

## Changelog

### v6.01
- **Group favorites**: Groups now use the same `FAVORITE_TAG` technique as scenes. Toggling a group's favorite in any client adds/removes the configured tag via `movieUpdate`. All group queries fetch `tags { name }` so `IsFavorite` is accurate in browse listings, latest items, and item detail responses. The global `Movie+IsFavorite` filter query uses `movie_filter: {tags: {value: $tid, modifier: INCLUDES}}` instead of returning an empty result.

### v6.00
- **Multi-client support**: Full compatibility with Infuse, SenPlayer; partial support for Swiftfin, Jellyfin Android, and Findroid
- **Swiftfin compatibility fixes**: Added alternate `/UserFavoriteItems/` routes used by Swiftfin for favorite toggling; fixed `ImageBlurHashes` on all BoxSet folder items so Swiftfin loads images for Performers, Studios, Groups, Tags, and Filters
- **Play/resume/watched sync**: Playback state fully synced with Stash — `play_count`, `resume_time`, and `last_played_at` are read from Stash and written back on stop. Scenes watched >90% are auto-marked played with resume cleared; otherwise resume position is saved
- **Favorites redesign**: Replaced broken `organized`-based approach with tag-based favorites for scenes (`FAVORITE_TAG` config); performers use Stash's native `favorite` field; studios use `studioUpdate` mutation; all favorites toggle correctly from any supported client
- **Duration fix**: `RunTimeTicks` now always included in `MediaSources` responses; fixed Swiftfin showing "0m" on play button. Stopped handler looks up actual duration from Stash when client sends 0
- **Android client support**: Rewrote case-insensitive path middleware to correctly handle parameterized routes with any casing; added `/ClientLog/Document` endpoint required by Jellyfin Android during startup
- **Rich media metadata**: Codec, resolution, bitrate, frame rate, and channel layout included in `MediaStreams` for accurate client display

### v5.04
- Sorting support for all listings: Performers, studios, groups, tags, and saved filter results now respond to client sort selection (name, date added, rating, random)
- Removed genre/tag cap: Genre endpoint returns all tags with scenes

### v5.03
- Fixed scenes failing to load caused by partial dates producing invalid ISO 8601 timestamps
- Fixed performer `PrimaryImageTag` set to null for performers without images

### v5.02
- Rich MediaStreams metadata (codec details, resolution, bitrate, channel layout)
- Subtitle support with SRT/VTT delivery
- Saved Filters browsing support
- Performer/Studio/Group image serving
- Tag-based library folders (TAG_GROUPS)

### v5.00
- Initial release with full Jellyfin API emulation
- Stash GraphQL integration for scenes, performers, studios, groups, tags
- Web configuration UI with dashboard, settings, and log viewer
- Docker support with PUID/PGID

## License

MIT License - Free to use and modify.
