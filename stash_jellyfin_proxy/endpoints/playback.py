"""Playback info endpoint — tells Jellyfin clients how to play a scene.

Returns the MediaSources + MediaStreams structure the client uses to pick
a stream URL and subtitle track. Every scene is reported as
Direct-Play-capable; we don't transcode. MediaStreams order is fixed:
index 0 is video, index 1 is audio (even if the scene has no audio track,
we synthesize a stereo AAC stream so Jellyfin clients that refuse 0-audio
payloads still play), then one entry per caption.
"""
import logging
import os

from starlette.responses import JSONResponse

from stash_jellyfin_proxy.stash.client import stash_query

logger = logging.getLogger("stash-jellyfin-proxy")


_LANG_NAMES = {
    "en": "English", "de": "German", "es": "Spanish",
    "fr": "French", "it": "Italian", "nl": "Dutch",
    "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ru": "Russian", "und": "Unknown",
}


def _empty_source(item_id: str) -> dict:
    return {
        "MediaSources": [{
            "Id": item_id or "src1",
            "Protocol": "File",
            "MediaStreams": [],
            "SupportsDirectPlay": True,
            "SupportsTranscoding": False,
        }],
        "PlaySessionId": "session-1",
    }


async def endpoint_playback_info(request):
    """`POST|GET /Items/{item_id}/PlaybackInfo` — return MediaSources +
    MediaStreams for the requested scene."""
    item_id = request.path_params.get("item_id")

    if not item_id or not item_id.startswith("scene-"):
        return JSONResponse(_empty_source(item_id))

    numeric_id = item_id.replace("scene-", "")
    result = await stash_query(
        """query FindScene($id: ID!) {
            findScene(id: $id) {
                id title
                files { path basename duration size video_codec audio_codec width height frame_rate bit_rate }
                captions { language_code caption_type }
            }
        }""",
        {"id": numeric_id},
    )
    scene = result.get("data", {}).get("findScene") if result else None
    if not scene:
        return JSONResponse(_empty_source(item_id))

    files = scene.get("files", [])
    file_data = files[0] if files else {}
    path = file_data.get("path", "")
    duration = float(file_data.get("duration") or 0)
    captions = scene.get("captions") or []

    video_codec = (file_data.get("video_codec") or "h264").lower()
    audio_codec = (file_data.get("audio_codec") or "").lower()
    vid_width = file_data.get("width") or 0
    vid_height = file_data.get("height") or 0
    frame_rate = file_data.get("frame_rate") or 0
    bit_rate = file_data.get("bit_rate") or 0
    file_size = file_data.get("size") or 0

    container = "mp4"
    if path:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in ("mkv", "avi", "wmv", "flv", "webm", "mov", "ts", "m4v", "mp4"):
            container = ext

    video_stream = {
        "Index": 0,
        "Type": "Video",
        "Codec": video_codec,
        "IsDefault": True,
        "IsForced": False,
        "IsExternal": False,
    }
    if vid_width and vid_height:
        video_stream["Width"] = vid_width
        video_stream["Height"] = vid_height
        video_stream["AspectRatio"] = f"{vid_width}:{vid_height}"
    if bit_rate:
        video_stream["BitRate"] = bit_rate
    if frame_rate:
        video_stream["RealFrameRate"] = frame_rate
        video_stream["AverageFrameRate"] = frame_rate

    media_streams = [video_stream]

    # Synthesize audio stream even when the scene has no audio_codec — some
    # clients refuse to play a MediaSource with zero audio streams.
    effective_audio_codec = audio_codec if audio_codec else "aac"
    media_streams.append({
        "Index": 1,
        "Type": "Audio",
        "Codec": effective_audio_codec,
        "Language": "und",
        "DisplayLanguage": "Unknown",
        "IsDefault": True,
        "IsForced": False,
        "IsExternal": False,
        "IsInterlaced": False,
        "IsTextSubtitleStream": False,
        "SupportsExternalStream": False,
        "DisplayTitle": f"{effective_audio_codec.upper()} - Stereo",
        "Channels": 2,
        "ChannelLayout": "stereo",
        "SampleRate": 48000,
    })

    for idx, caption in enumerate(captions):
        lang_code = caption.get("language_code", "und")
        caption_type = (caption.get("caption_type", "") or "").lower()
        if caption_type not in ("srt", "vtt"):
            caption_type = "vtt"
        codec = "srt" if caption_type == "srt" else "webvtt"
        display_lang = _LANG_NAMES.get(lang_code, lang_code.upper())

        media_streams.append({
            "Index": 2 + idx,
            "Type": "Subtitle",
            "Codec": codec,
            "Language": lang_code,
            "DisplayLanguage": display_lang,
            "DisplayTitle": f"{display_lang} ({caption_type.upper()})",
            "Title": display_lang,
            "IsDefault": idx == 0,
            "IsForced": False,
            "IsExternal": True,
            "IsTextSubtitleStream": True,
            "SupportsExternalStream": True,
            "DeliveryMethod": "External",
            "DeliveryUrl": f"Subtitles/{idx + 1}/0/Stream.{caption_type}",
        })

    logger.debug(f"PlaybackInfo for {item_id}: {len(captions)} subtitles")

    runtime_ticks = int(duration * 10000000) if duration else 0
    media_source = {
        "Id": item_id,
        "Name": scene.get("title") or os.path.basename(path),
        "Path": path,
        "Protocol": "File",
        "Type": "Default",
        "Container": container,
        "RunTimeTicks": runtime_ticks,
        "Size": int(file_size) if file_size else 0,
        "Bitrate": bit_rate if bit_rate else 0,
        "SupportsDirectPlay": True,
        "SupportsDirectStream": True,
        "SupportsTranscoding": False,
        "MediaStreams": media_streams,
        "DefaultAudioStreamIndex": 1,
        "DefaultSubtitleStreamIndex": -1,
    }

    return JSONResponse({
        "MediaSources": [media_source],
        "PlaySessionId": f"session-{item_id}",
    })
