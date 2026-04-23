"""User-identity endpoints — authenticate, list, and fetch user profiles.

All user mutation endpoints (favorites, played state, ratings) live in
proxy/endpoints/user_actions.py; this module covers identity reads and
the primary authentication flow.
"""
import json
import logging
import os
import re
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from proxy import runtime
from proxy.mapping.user import build_user_dto
from proxy.middleware.auth import get_client_ip, clear_ip_failures
from proxy.state.stats import record_auth_attempt
from proxy.util.images import generate_text_icon

logger = logging.getLogger("stash-jellyfin-proxy")


def parse_emby_auth_header(request: Request) -> dict:
    """Extract Client / Device / DeviceId / Version from Jellyfin/Emby
    auth headers. Returns a dict with defaults for missing fields so
    downstream SessionInfo construction has everything it needs."""
    info = {"Client": "Jellyfin", "DeviceName": "Unknown", "DeviceId": "", "ApplicationVersion": "0.0.0"}
    auth_header = ""
    for key, value in request.headers.items():
        if key.lower() in ("authorization", "x-emby-authorization"):
            auth_header = value
            break
    if auth_header:
        for field, json_key in [
            ("Client", "Client"),
            ("Device", "DeviceName"),
            ("DeviceId", "DeviceId"),
            ("Version", "ApplicationVersion"),
        ]:
            match = re.search(rf'{field}="([^"]*)"', auth_header)
            if match:
                info[json_key] = match.group(1)
    return info


async def endpoint_authenticate_by_name(request: Request):
    """`POST /Users/AuthenticateByName` — password login. Returns a full
    AuthenticationResult with User + SessionInfo + AccessToken."""
    if request.method == "GET":
        return Response(status_code=405, headers={"Allow": "POST"})

    try:
        data = await request.json()
    except Exception:
        data = {}

    username = data.get("Username", "User")
    pw = data.get("Pw", "")

    logger.info(f"Auth attempt for user: {username}")
    logger.debug(f"Auth password check: input len={len(pw)}, expected len={len(runtime.SJS_PASSWORD)}")

    if pw.strip() == runtime.SJS_PASSWORD.strip():
        client_ip = get_client_ip(request.scope)
        clear_ip_failures(client_ip)
        logger.debug(f"Cleared auth failure tracking for {client_ip} after successful login")

        record_auth_attempt(success=True)
        logger.info(f"Auth SUCCESS for user {runtime.SJS_USER}")
        client_info = parse_emby_auth_header(request)
        session_id = str(uuid.uuid4())
        auth_response = {
            "User": build_user_dto(username),
            "SessionInfo": {
                "Id": session_id,
                "UserId": runtime.USER_ID,
                "UserName": username,
                "Client": client_info["Client"],
                "DeviceName": client_info["DeviceName"],
                "DeviceId": client_info["DeviceId"],
                "ApplicationVersion": client_info["ApplicationVersion"],
                "RemoteEndPoint": client_ip,
                "IsActive": True,
                "SupportsMediaControl": False,
                "SupportsRemoteControl": False,
                "HasCustomDeviceName": False,
                "LastActivityDate": "2024-01-01T00:00:00.0000000Z",
                "LastPlaybackCheckIn": "0001-01-01T00:00:00.0000000Z",
                "PlayState": {
                    "CanSeek": False,
                    "IsPaused": False,
                    "IsMuted": False,
                    "RepeatMode": "RepeatNone",
                    "PlaybackOrder": "Default",
                    "PositionTicks": 0,
                    "VolumeLevel": 100,
                },
                "Capabilities": {
                    "PlayableMediaTypes": ["Audio", "Video"],
                    "SupportedCommands": [],
                    "SupportsMediaControl": False,
                    "SupportsContentUploading": False,
                    "SupportsPersistentIdentifier": True,
                    "SupportsSync": False,
                },
                "PlayableMediaTypes": ["Audio", "Video"],
                "AdditionalUsers": [],
                "NowPlayingQueue": [],
                "NowPlayingQueueFullItems": [],
                "SupportedCommands": [],
                "ServerId": runtime.SERVER_ID,
            },
            "AccessToken": runtime.ACCESS_TOKEN,
            "ServerId": runtime.SERVER_ID,
        }
        auth_json = json.dumps(auth_response, indent=2)
        logger.debug(f"Auth response ({len(auth_json)} bytes): {auth_json[:200]}...")
        try:
            cf = runtime.CONFIG_FILE
            debug_path = os.path.join(os.path.dirname(cf) if cf else "/config", "auth_debug.json")
            with open(debug_path, "w") as f:
                f.write(auth_json)
            logger.debug(f"Full auth response written to {debug_path}")
        except Exception as e:
            logger.debug(f"Could not write auth debug file: {e}")
        return JSONResponse(auth_response)

    record_auth_attempt(success=False)
    logger.warning("Auth FAILED - Invalid Key")
    return JSONResponse({"error": "Invalid Token"}, status_code=401)


async def endpoint_users(request):
    """`GET /Users` alias that returns just the single user."""
    return JSONResponse([build_user_dto()])


async def endpoint_user_by_id(request):
    """`GET /Users/{user_id}` — single-user proxy ignores the path
    param and returns the configured user."""
    return JSONResponse(build_user_dto())


async def endpoint_user_me(request):
    """`GET /Users/Me` — current user."""
    return JSONResponse(build_user_dto())


async def endpoint_user_image(request):
    """`GET /UserImage` and `GET /Users/{user_id}/Images/Primary` —
    Swiftfin displays this on the pre-login screen."""
    img_data, content_type = generate_text_icon(runtime.SJS_USER or "?", width=200, height=200)
    return Response(content=img_data, media_type=content_type)
