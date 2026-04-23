"""Pure-ASGI request logging middleware + live stream-session tracking.

Intentionally NOT a BaseHTTPMiddleware subclass — that one wraps the
response body, which breaks streaming responses. This implementation
forwards the send channel untouched so /Videos/{id}/stream byte pumps
work cleanly.

Stream lifecycle events (▶ started, ⏸ resumed post-restart, continue,
expire) feed the `_active_streams` dict that /api/streams exposes on
the Web UI Dashboard.
"""
import logging
import re
import time

from proxy import runtime
from proxy.state import streams as _streams
from proxy.state import stats as _stats
from proxy.stash.scene import get_scene_info

logger = logging.getLogger("stash-jellyfin-proxy")


class RequestLoggingMiddleware:
    """Pure ASGI middleware that doesn't wrap streaming responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        path = scope.get("path", "")
        query_string = scope.get("query_string", b"").decode()
        client = scope.get("client", ("unknown", 0))
        client_host = client[0] if client else "unknown"

        # Log arrival (for in-flight visibility) before dispatching.
        if path not in ("/", "/favicon.ico") and not path.startswith("/ui"):
            full_path = f"{path}?{query_string}" if query_string else path
            logger.debug(f"→ {scope.get('method', 'GET')} {full_path}")

        headers = {}
        for key, value in scope.get("headers", []):
            headers[key.decode().lower()] = value.decode()

        is_stream = "/stream" in path.lower() or "/Videos/" in path

        # Track stream events at request ARRIVAL, not completion. A long-
        # lived range request for a full scene can hold the connection
        # open for the entire video duration — if we wait for it to close
        # before populating _active_streams, the Dashboard stays empty
        # the whole time the client is actually streaming.
        if is_stream:
            self._track_stream_event(path, headers, client_host, ms=0)

        # Capture status off the http.response.start message.
        response_status = [0]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_status[0] = message.get("status", 0)
            try:
                await send(message)
            except Exception:
                # Ignore send errors (client disconnected mid-stream)
                pass

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            error_str = str(e).lower()
            if "content-length" in error_str or "disconnect" in error_str or "cancelled" in error_str:
                pass
            else:
                process_time = time.time() - start_time
                ms = int(process_time * 1000)
                logger.error(f"{path} -> ERROR ({ms}ms): {str(e)}")
                return

        process_time = time.time() - start_time
        ms = int(process_time * 1000)
        status = response_status[0]

        is_error = status >= 400
        is_auth = "/Authenticate" in path
        is_slow = ms > 1000

        if is_stream:
            if is_error and status > 0:
                logger.warning(f"{path} -> {status} ({ms}ms)")
            # Completion-side: keep the stream's last_seen fresh. The
            # tracking record already exists (populated on arrival above).
            entry = _streams._active_streams.get(self._scene_id(path))
            if entry is not None:
                entry["last_seen"] = time.time()
        elif is_error and status > 0:
            logger.warning(f"{path} -> {status} ({ms}ms)")
        elif is_auth:
            logger.debug(f"Login request completed -> {status} ({ms}ms)")
        elif is_slow:
            logger.info(f"Slow request: {path} ({ms}ms)")
        elif status > 0:
            logger.debug(f"{path} -> {status} ({ms}ms)")

    @staticmethod
    def _scene_id(path: str) -> str:
        match = re.search(r'/(scene-\d+)/', path)
        return match.group(1) if match else "unknown"

    def _track_stream_event(self, path: str, headers: dict, client_host: str, ms: int) -> None:
        """Extract scene + client identity from a /Videos/... request and
        update _active_streams / _client_streams / stream counters
        accordingly. Logs the lifecycle boundary (▶ started, ⏸ resume,
        continue, expire) for operator visibility."""
        match = re.search(r'/(scene-\d+)/', path)
        scene_id = match.group(1) if match else "unknown"
        now = time.time()

        user_match = re.search(r'/Users/([^/]+)/', path)
        user = user_match.group(1) if user_match else runtime.SJS_USER

        client_ip = headers.get("x-forwarded-for", client_host).split(",")[0].strip()
        user_agent = headers.get("user-agent", "")
        if "Infuse" in user_agent:
            client_type = "Infuse"
        elif "VLC" in user_agent:
            client_type = "VLC"
        elif "Jellyfin" in user_agent:
            client_type = "Jellyfin"
        else:
            client_type = user_agent.split("/")[0][:20] if user_agent else "Unknown"

        range_header = headers.get("range", "")
        byte_position = 0
        if range_header.startswith("bytes="):
            try:
                byte_position = int(range_header[6:].split("-")[0])
            except (ValueError, IndexError):
                pass

        client_key = f"{client_ip}|{client_type}"

        stream_info = _streams._active_streams.get(scene_id)

        # File size is needed by should_count_as_new_stream to classify
        # mid-file trailing requests; cache it on the stream record.
        cached_file_size = stream_info.get("file_size", 0) if stream_info else 0
        if not cached_file_size:
            cached_file_size = get_scene_info(scene_id).get("file_size", 0)

        should_count, is_trailing_after_restart = _streams.should_count_as_new_stream(
            scene_id, client_ip, byte_position, cached_file_size,
        )

        if should_count:
            _stats.reset_daily_stats_if_needed()
            _stats._proxy_stats["total_streams"] += 1
            _stats._proxy_stats["streams_today"] += 1
            if client_ip not in _stats._proxy_stats["unique_ips_today"]:
                _stats._proxy_stats["unique_ips_today"].append(client_ip)
            _stats.mark_dirty()
            _stats.maybe_save_stats()

        # 30+ min silence = UI should see "new stream", not "continue".
        if stream_info and (now - stream_info["last_seen"]) >= _streams.STREAM_COUNT_COOLDOWN:
            logger.debug(f"Stream expired for {scene_id}: {int((now - stream_info['last_seen'])/60)}min gap")
            _streams.mark_stream_stopped(scene_id, from_stop_notification=False)
            stream_info = None

        if stream_info is None:
            stopped_at = _streams._recently_stopped.get(scene_id)
            if stopped_at and (now - stopped_at) < _streams.RECENTLY_STOPPED_GRACE:
                logger.debug(f"Ignoring trailing request for recently stopped stream: {scene_id}")
            elif is_trailing_after_restart:
                scene_info = get_scene_info(scene_id)
                title = scene_info.get("title", scene_id)
                _streams._active_streams[scene_id] = {
                    "last_seen": now,
                    "started": now,
                    "title": title,
                    "performer": scene_info.get("performer", ""),
                    "user": user,
                    "client_ip": client_ip,
                    "client_type": client_type,
                    "client_key": client_key,
                    "file_size": cached_file_size,
                }
                _streams._client_streams[client_key] = scene_id
                logger.info(f"⏸ Stream resuming (post-restart): {title} ({scene_id}) from {client_ip}")
            else:
                _streams.cancel_client_streams(client_key, scene_id)
                if scene_id in _streams._recently_stopped:
                    del _streams._recently_stopped[scene_id]

                scene_info = get_scene_info(scene_id)
                title = scene_info.get("title", scene_id)
                performer = scene_info.get("performer", "")
                duration = scene_info.get("duration", 0)
                file_size = scene_info.get("file_size", 0)
                _streams._active_streams[scene_id] = {
                    "last_seen": now,
                    "started": now,
                    "title": title,
                    "performer": performer,
                    "user": user,
                    "client_ip": client_ip,
                    "client_type": client_type,
                    "client_key": client_key,
                    "file_size": file_size,
                }
                _streams._client_streams[client_key] = scene_id
                _stats.record_play_count(scene_id, title, performer, client_ip, duration)
                logger.info(f"▶ Stream started: {title} ({scene_id}) by {user} from {client_ip} [{client_type}]")
        elif (now - stream_info["last_seen"]) > _streams.STREAM_RESUME_THRESHOLD:
            gap = int(now - stream_info["last_seen"])
            stream_info["last_seen"] = now
            logger.info(f"▶ Stream resumed: {stream_info['title']} ({scene_id}, paused {gap}s)")
        else:
            stream_info["last_seen"] = now
            logger.debug(f"Stream continue: {scene_id} ({ms}ms)")
