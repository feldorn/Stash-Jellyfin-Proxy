"""System-info endpoints — identity and capability reporting.

Clients parse LocalAddress and will refuse the server if it advertises
something unreachable (e.g. http://0.0.0.0:8096). Respect reverse-proxy
headers so SWAG/nginx setups advertise the public https origin.
"""
from starlette.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

from proxy import runtime


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
    """`GET /` — Infuse probes root for life. Redirect to System/Info/Public
    so the response also satisfies clients that expect server identity here."""
    return RedirectResponse(url="/System/Info/Public")


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
