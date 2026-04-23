"""Live stream tracking — active-stream registry and session-counting
heuristics used by the Dashboard and by the request-logging middleware.

Module-level state:
    _active_streams    : scene_id → stream record (title, IP, client_type, …)
    _client_streams    : client_key → scene_id (single-stream-per-client)
    _recently_stopped  : scene_id → stop-time (race guard after stop notice)
    _stream_positions  : (scene_id, client_ip) → position snapshot for
                         should_count_as_new_stream()

Callers read/write via `streams._active_streams[…]` — reference identity
of every dict is preserved. `mark_stream_stopped` and `cancel_client_streams`
are the only methods that remove entries; read-only consumers (the
Dashboard's /api/streams endpoint) iterate freely.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger("stash-jellyfin-proxy")

# Thresholds used by should_count_as_new_stream + mark_stream_stopped.
STREAM_RESUME_THRESHOLD = 90   # seconds of idle before "resume"
RECENTLY_STOPPED_GRACE = 5     # seconds to ignore new requests post-stop
STREAM_COUNT_COOLDOWN = 1800   # 30 min — always count as new after
STREAM_START_GAP = 300         # 5 min — min gap for "seek to start"
STREAM_START_THRESHOLD = 0.05  # first 5% of file considered "start"

# Live state — mutated in place by callers.
_active_streams = {}    # scene_id → {"last_seen", "started", "title", "user", …}
_client_streams = {}    # client_key → scene_id
_recently_stopped = {}  # scene_id → unix timestamp when stopped
_stream_positions = {}  # (scene_id, client_ip) → {"last_position", "last_time", "file_size"}


def should_count_as_new_stream(scene_id: str, client_ip: str, byte_position: int, file_size: int) -> tuple:
    """Decide whether this stream request should count as a new stream.

    Uses position + timing heuristics:
      * 30+ min since last activity → always new stream
      * Seek to start (first 5%) with 5+ min gap → new stream
      * First request at start of file → new stream
      * First request mid-file → likely post-restart trailing, NOT new
      * Otherwise (seeking within same session) → not new

    Returns (should_count, is_trailing_after_restart).
    """
    position_key = (scene_id, client_ip)
    now = time.time()

    if position_key not in _stream_positions:
        _stream_positions[position_key] = {
            "last_position": byte_position,
            "last_time": now,
            "file_size": file_size,
        }
        if file_size > 0:
            position_ratio = byte_position / file_size
            if position_ratio > STREAM_START_THRESHOLD:
                logger.debug(f"Ignoring mid-file first request for {scene_id}: position {position_ratio:.1%} (likely post-restart trailing request)")
                return (False, True)
        elif byte_position > 0:
            logger.debug(f"Ignoring non-zero first request for {scene_id}: position {byte_position} bytes, unknown size (likely post-restart)")
            return (False, True)
        return (True, False)

    last_info = _stream_positions[position_key]
    elapsed = now - last_info["last_time"]

    _stream_positions[position_key] = {
        "last_position": byte_position,
        "last_time": now,
        "file_size": file_size or last_info["file_size"],
    }

    if elapsed >= STREAM_COUNT_COOLDOWN:
        logger.debug(f"New stream counted for {scene_id}: {int(elapsed/60)}min gap exceeds cooldown")
        return (True, False)

    effective_file_size = file_size or last_info["file_size"]
    if effective_file_size > 0:
        position_ratio = byte_position / effective_file_size
        is_at_start = position_ratio <= STREAM_START_THRESHOLD
        has_sufficient_gap = elapsed >= STREAM_START_GAP

        if is_at_start and has_sufficient_gap:
            logger.debug(f"New stream counted for {scene_id}: seek to start ({position_ratio:.1%}) with {int(elapsed/60)}min gap")
            return (True, False)
        elif is_at_start:
            logger.debug(f"Seek to start ignored for {scene_id}: only {int(elapsed)}s gap (need {STREAM_START_GAP}s)")

    logger.debug(f"Same stream session for {scene_id}: position {byte_position}, {int(elapsed)}s since last")
    return (False, False)


def mark_stream_stopped(scene_id: str, from_stop_notification: bool = False) -> None:
    """Mark a stream as stopped so the next request is treated as a new start."""
    if scene_id in _active_streams:
        stream_info = _active_streams[scene_id]
        client_key = stream_info.get("client_key")
        if client_key and _client_streams.get(client_key) == scene_id:
            del _client_streams[client_key]
        del _active_streams[scene_id]

    if from_stop_notification:
        _recently_stopped[scene_id] = time.time()
        # GC expired entries so the dict doesn't grow unbounded.
        now = time.time()
        expired = [k for k, v in _recently_stopped.items() if now - v > RECENTLY_STOPPED_GRACE * 2]
        for k in expired:
            del _recently_stopped[k]


def cancel_client_streams(client_key: str, new_scene_id: Optional[str] = None) -> list:
    """Cancel any active streams from this client (except new_scene_id).
    Returns the list of scene_ids cancelled."""
    cancelled = []
    current_scene = _client_streams.get(client_key)
    if current_scene and current_scene != new_scene_id:
        if current_scene in _active_streams:
            old_info = _active_streams[current_scene]
            logger.info(f"⏹ Stream cancelled: {old_info.get('title', current_scene)} ({current_scene}) - client started new video")
            del _active_streams[current_scene]
            cancelled.append(current_scene)
        del _client_streams[client_key]
    return cancelled
