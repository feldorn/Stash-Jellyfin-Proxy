"""System-info endpoints — identity and capability reporting.

Clients parse LocalAddress and will refuse the server if it advertises
something unreachable (e.g. http://0.0.0.0:8096). Respect reverse-proxy
headers so SWAG/nginx setups advertise the public https origin.
"""
from starlette.responses import JSONResponse, Response
from starlette.requests import Request

from stash_jellyfin_proxy import runtime


_ROOT_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Stash-Jellyfin Proxy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: -apple-system, system-ui, sans-serif; background: #1a1a1f; color: #ccc;
       max-width: 540px; margin: 60px auto; padding: 0 20px; line-height: 1.5; }
h1 { color: #eee; font-weight: 500; margin-bottom: 4px; }
.sub { color: #888; margin-bottom: 30px; font-size: 14px; }
.card { background: #252530; border: 1px solid #333; border-radius: 8px; padding: 16px 20px; margin: 12px 0; }
code { background: #333; padding: 2px 6px; border-radius: 3px; font-size: 13px; }
a { color: #5ba8ff; }
</style>
</head>
<body>
<h1>Stash-Jellyfin Proxy</h1>
<div class="sub">Jellyfin API emulation layer in front of a Stash media server.</div>
<div class="card">
  <strong>API endpoint.</strong>
  This server works only with a dedicated Jellyfin-compatible
  <em>media player</em> &mdash; <em>Swiftfin</em> (iOS / iPadOS / tvOS),
  <em>Infuse</em> (iOS / tvOS), or <em>SenPlayer</em> (iOS).
  The official Jellyfin mobile apps are not supported because they load
  the server's web UI in a WebView rather than using the API directly.
</div>
<div class="card">
  Configuration and status dashboard: <a href="/"></a>
  <script>document.querySelector('.card a').href =
    window.location.origin.replace(/:\\d+/, ':' + "__UI_PORT__");
    document.querySelector('.card a').textContent = document.querySelector('.card a').href;
  </script>
</div>
</body>
</html>
"""


def derive_local_address(request: Request) -> str:
    """Build the externally-visible base URL the client used to reach us."""
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if fwd_proto and fwd_host:
        return f"{fwd_proto}://{fwd_host}"
    if fwd_host:
        scheme = request.url.scheme or "http"
        return f"{scheme}://{fwd_host}"
    return f"http://{runtime.PROXY_BIND}:{runtime.PROXY_PORT}"


async def endpoint_root(request):
    """`GET /` — human-readable landing page identifying the proxy.

    Previously redirected to /System/Info/Public so clients that probe
    the root URL for server identity would see it. That broke the
    official Jellyfin iOS/iPadOS app: it hits `/` on first connect,
    followed our 307, and rendered the resulting JSON as its "server
    content" view — user sees raw JSON and gets stuck.

    Now serves a small HTML page. Clients that want identity hit
    `/System/Info/Public` explicitly (every maintained Jellyfin client
    does). Infuse's liveness probe is satisfied by any 2xx response."""
    html = _ROOT_LANDING_HTML.replace("__UI_PORT__", str(runtime.UI_PORT))
    return Response(content=html, media_type="text/html")


async def endpoint_system_info(request):
    """`GET /System/Info` — full server info (auth-gated in spec but clients
    sometimes probe it unauthenticated; we serve the same shape either way)."""
    local_addr = derive_local_address(request)
    return JSONResponse({
        "ServerName": runtime.SERVER_NAME,
        "Version": runtime.JELLYFIN_VERSION,
        "Id": runtime.SERVER_ID,
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "StartupWizardCompleted": True,
        "SupportsLibraryMonitor": False,
        "WebSocketPortNumber": runtime.PROXY_PORT,
        "CompletedInstallations": [],
        "CanSelfRestart": False,
        "CanLaunchWebBrowser": False,
        "HasPendingRestart": False,
        "HasUpdateAvailable": False,
        "IsShuttingDown": False,
        "TranscodingTempPath": "/tmp",
        "LogPath": "/tmp",
        "InternalMetadataPath": "/tmp",
        "CachePath": "/tmp",
        "ProgramDataPath": "/tmp",
        "ItemsByNamePath": "/tmp",
        "LocalAddress": local_addr,
    })


async def endpoint_public_info(request):
    """`GET /System/Info/Public` — pre-auth identity probe. The response
    shape is what every Jellyfin client uses to validate the server at
    add-time, so field set and LocalAddress accuracy matter."""
    return JSONResponse({
        "LocalAddress": derive_local_address(request),
        "ServerName": runtime.SERVER_NAME,
        "Version": runtime.JELLYFIN_VERSION,
        "Id": runtime.SERVER_ID,
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "StartupWizardCompleted": True,
    })
