# Stash-Jellyfin Proxy

**Current Version: v3.92**

## Overview
The Stash-Jellyfin Proxy is a Python-based proxy server designed to bridge the gap between Stash media server and Jellyfin-compatible media players like Infuse. It achieves this by emulating the Jellyfin API, allowing users to access their Stash content through familiar Jellyfin client interfaces. The project aims to provide a seamless viewing experience for Stash users who prefer the robust client ecosystem of Jellyfin. Key capabilities include comprehensive Jellyfin API emulation, robust Stash GraphQL integration, and a user-friendly web interface for configuration and monitoring.

## User Preferences
Preferred communication style: Simple, everyday language.

## System Architecture
The proxy is built on a Python backend using the Starlette ASGI framework with Hypercorn as the server. It listens on port 8096 (default Jellyfin port). Core components include a comprehensive suite of Jellyfin API endpoints, a Stash GraphQL client with retry logic, a filter transformer for Stash saved filters, an image handler with caching and optional resizing, and a stream proxy for video content. The system supports tag-based library folders, latest item displays, and provides a web-based UI for configuration, status monitoring, and log viewing. Authentication uses simple shared credentials. Docker containerization is supported for easy deployment, including environment variable overrides and health checks.

### UI/UX
An embedded Web UI is served from the Python script on UI_PORT (default 8097). It features a dashboard displaying proxy status, Stash connection health, and active streams. A configuration editor allows management of all settings, and a log viewer provides filtering and download capabilities.

### Technical Implementations
- **Jellyfin API Emulation**: Supports over 50 Jellyfin endpoints to ensure broad client compatibility, including authentication, library browsing (Scenes, Performers, Studios, Groups), video streaming with subtitle support, and image serving.
- **Stash Integration**: Utilizes GraphQL API for all content types, transforms Stash saved filters, handles pagination, and enables tag-based library organization.
- **Configuration**: Managed via `stash_jellyfin_proxy.conf` with extensive settings for Stash connection, proxy behavior, logging, and security. Most settings apply dynamically without server restart.
- **Security**: Implements IP-based security with auto-banning for failed authentication attempts and requires an ACCESS_TOKEN for protected endpoints.
- **Streaming**: Uses chunked streaming for video content and includes enhanced stream tracking on the dashboard with user, client IP, and client type information.

## External Dependencies
- **Stash Media Server**: The primary backend media server.
- **Python 3.8+**: Runtime environment.
- **hypercorn**: ASGI server for the Python application.
- **starlette**: Web framework for building the API and UI.
- **requests**: HTTP client for interacting with the Stash API.
- **Pillow**: (Optional) For image resizing functionalities.