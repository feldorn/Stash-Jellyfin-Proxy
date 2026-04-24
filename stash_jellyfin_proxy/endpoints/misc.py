"""Miscellaneous small endpoints that don't belong to a larger module.

`endpoint_display_preferences` — the Jellyfin client preferences blob.
`endpoint_websocket` — keepalive WebSocket so Infuse/Swiftfin don't hang.
"""
import asyncio
import logging

from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

logger = logging.getLogger("stash-jellyfin-proxy")


async def endpoint_display_preferences(request):
    """`GET|POST /DisplayPreferences/{prefs_id}` — client UI preferences.
    POST simply echoes the ID; GET returns a static config with home-page
    section layout that shows the library tiles + latest-media rail."""
    prefs_id = request.path_params.get("prefs_id", "usersettings")

    if request.method == "POST":
        return JSONResponse({"Id": prefs_id})

    return JSONResponse({
        "Id": prefs_id,
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {
            "homesection0": "smalllibrarytiles",
            "homesection1": "latestmedia",
            "homesection2": "nextup",
            "homesection3": "none",
            "homesection4": "none",
            "homesection5": "none",
            "homesection6": "none",
        },
        "ScrollDirection": "Horizontal",
        "ShowBackdrop": True,
        "RememberSorting": False,
        "ShowSidebar": False,
    })


async def endpoint_websocket(websocket: WebSocket):
    """`GET /socket` — Jellyfin keepalive WebSocket.

    Accepts the connection and runs a keepalive loop matching Jellyfin's
    protocol. Without this, Infuse-Direct hangs ~3s after login and then
    retries indefinitely."""
    await websocket.accept()
    logger.debug(f"WebSocket connected: path={websocket.url.path} from {websocket.client}")
    try:
        await websocket.send_json({"MessageType": "ForceKeepAlive", "Data": 30})
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=60.0)
                if msg.get("MessageType") == "KeepAlive":
                    await websocket.send_json({"MessageType": "KeepAlive"})
            except asyncio.TimeoutError:
                await websocket.send_json({"MessageType": "KeepAlive"})
    except Exception as e:
        logger.debug(f"WebSocket disconnected: {e}")
