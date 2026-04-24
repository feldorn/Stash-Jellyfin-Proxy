"""Scene-to-Jellyfin-Item mapping.

Takes a Stash scene GraphQL object and produces the flat Jellyfin Item
dict that every list/detail endpoint returns. Reads SERVER_ID and
FAVORITE_TAG from stash_jellyfin_proxy.runtime.

This is the central mapper — any change to its output shape will be
caught by the characterization suite against real Stash data. Keep
logic changes minimal unless the characterization baseline has been
rebuilt with expected-diff annotations.
"""
import hashlib
import os
from typing import Any, Dict, Optional

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.genre import compute_genres
from stash_jellyfin_proxy.util.series import parse_episode
from stash_jellyfin_proxy.util.sort import sort_name_for


def is_series_scene(scene: Dict[str, Any]) -> bool:
    """A scene is a Series Episode if its studio (or any ancestor studio)
    is tagged with runtime.SERIES_TAG. Per audit §3.5 consistency rule:
    SERIES-tagged studio → Episode type in every context."""
    if not runtime.SERIES_TAG:
        return False
    tag_name = runtime.SERIES_TAG
    studio = scene.get("studio") or {}
    # Walk the studio chain: studio itself, then parent_studio recursively.
    while studio:
        for t in studio.get("tags") or []:
            if (t.get("name") or "").lower() == tag_name.lower():
                return True
        studio = studio.get("parent_studio")
    return False


def is_scene_favorite(scene: Dict[str, Any]) -> bool:
    """Check if a scene has the configured favorite tag."""
    if not runtime.FAVORITE_TAG:
        return False
    tag_names = [t.get("name", "") for t in scene.get("tags", [])]
    return runtime.FAVORITE_TAG in tag_names


def is_group_favorite(group: Dict[str, Any]) -> bool:
    """Check if a group has the configured favorite tag."""
    if not runtime.FAVORITE_TAG:
        return False
    tag_names = [t.get("name", "") for t in group.get("tags", [])]
    return runtime.FAVORITE_TAG in tag_names


_GENRE_UNSET = object()


def format_jellyfin_item(
    scene: Dict[str, Any],
    parent_id: str = "root-scenes",
    genre_allowed=_GENRE_UNSET,
) -> Dict[str, Any]:
    """Build a Jellyfin Item dict from a Stash scene object."""
    raw_id = str(scene.get("id"))
    item_id = f"scene-{raw_id}"
    date = scene.get("date")
    files = scene.get("files", [])
    path = files[0].get("path") if files else ""
    raw_duration = files[0].get("duration") if files else None
    duration = float(raw_duration or 0) if files else 0

    # Title fallback: title -> code -> filename (without extension) -> Scene #
    title = scene.get("title") or scene.get("code")
    if not title and path:
        filename = os.path.basename(path)
        title = os.path.splitext(filename)[0] if filename else None
    if not title:
        title = f"Scene {raw_id}"
    studio = scene.get("studio", {}).get("name") if scene.get("studio") else None
    description = scene.get("details") or ""
    tags = scene.get("tags", [])
    performers = scene.get("performers", [])

    # Per-item unique image tags (Infuse caches image data by (ItemId,
    # ImageTag); shared tags across many items in a refreshing list
    # confuse its resolver and images end up never loading in rows like
    # Next Up).
    primary_tag = f"p{raw_id}"
    backdrop_tag = f"b{raw_id}"
    etag = hashlib.md5(
        f"{item_id}|{scene.get('play_count') or 0}|{scene.get('resume_time') or 0}|{scene.get('last_played_at') or ''}".encode()
    ).hexdigest()[:16]

    # Infuse ignores PlaybackPositionTicks on cold launch unless UserData
    # looks like a real Jellyfin UserItemDataDto: LastPlayedDate must be
    # a valid datetime (not ""), PlayedPercentage must be present when a
    # resume exists, and ItemId must accompany Key. Senplayer is lenient
    # here; Infuse is not.
    resume_seconds = float(scene.get("resume_time") or 0)
    play_count = scene.get("play_count") or 0
    last_played = scene.get("last_played_at") or None
    user_data = {
        "PlaybackPositionTicks": int(resume_seconds * 10000000),
        "PlayCount": play_count,
        "IsFavorite": is_scene_favorite(scene),
        "Played": play_count > 0,
        "Key": item_id,
        "ItemId": item_id,
    }
    if last_played:
        user_data["LastPlayedDate"] = last_played
    if resume_seconds > 0 and duration > 0:
        user_data["PlayedPercentage"] = min(100.0, (resume_seconds / duration) * 100.0)

    # Per audit §3.5: SERIES-tagged studio scenes are Episode type *everywhere*.
    # Consistency rule applies in browse, search, favorites, history — no
    # exceptions. Season/Episode numbers come from title parsing; unparseable
    # titles fall back to Season 0 so grouping still works.
    is_episode = is_series_scene(scene)
    item_type = "Episode" if is_episode else "Movie"

    item = {
        "Name": title,
        "SortName": sort_name_for(title),
        "Id": item_id,
        "Etag": etag,
        "ServerId": runtime.SERVER_ID,
        "Type": item_type,
        "IsFolder": False,
        "MediaType": "Video",
        "CanDownload": True,
        "ParentId": parent_id,
        "ImageTags": {"Primary": primary_tag},
        "BackdropImageTags": [backdrop_tag],
        "ImageBlurHashes": {"Primary": {primary_tag: "000000"}, "Backdrop": {backdrop_tag: "000000"}},
        "OfficialRating": runtime.OFFICIAL_RATING,
        "RunTimeTicks": int(duration * 10000000) if duration else 0,
        "UserData": user_data,
    }

    # StashDB provider id — lets Swiftfin/Infuse surface a "View on StashDB"
    # external-link button on the scene detail page. Omit if the scene has
    # no stash_ids entry.
    stash_ids = scene.get("stash_ids") or []
    if stash_ids:
        first = stash_ids[0] or {}
        sid = first.get("stash_id") if isinstance(first, dict) else None
        if sid:
            item["ProviderIds"] = {"StashDb": sid}

    if is_episode:
        studio_obj = scene.get("studio") or {}
        studio_id = studio_obj.get("id")
        studio_name = studio_obj.get("name") or ""
        parsed = parse_episode(title)
        season_num, episode_num = parsed if parsed else (0, 0)
        item["ParentIndexNumber"] = season_num
        item["IndexNumber"] = episode_num
        if studio_id:
            item["SeriesId"] = f"series-{studio_id}"
            item["SeriesName"] = studio_name
            item["SeasonId"] = f"season-{studio_id}-{season_num}"
            item["SeasonName"] = f"Season {season_num}" if season_num else "Specials"

    if date:
        item["ProductionYear"] = int(date[:4])
        if len(date) == 4:
            item["PremiereDate"] = f"{date}-01-01T00:00:00.0000000Z"
        elif len(date) == 7:
            item["PremiereDate"] = f"{date}-01T00:00:00.0000000Z"
        else:
            item["PremiereDate"] = f"{date}T00:00:00.0000000Z"

    # Overview is just the Stash `details` text now — the studio is already
    # exposed as a structured field (Studios[] below), appending "Studio: X"
    # here was redundant and cluttered the description.
    if description:
        item["Overview"] = description

    # Structured studio reference so clients can render it as a clickable
    # link in the About panel instead of as free-form text.
    studio_obj = scene.get("studio") or {}
    if studio_obj.get("name") and studio_obj.get("id"):
        item["Studios"] = [
            {"Name": studio_obj["name"], "Id": f"studio-{studio_obj['id']}"}
        ]

    if tags:
        tag_names = [t.get("name") for t in tags if t.get("name")]
        if genre_allowed is _GENRE_UNSET:
            genres, residual = compute_genres(tag_names)
        else:
            genres, residual = compute_genres(tag_names, genre_allowed)
        # Always populate both. Clients that filter by Tags vs Genres see
        # the split; clients that only look at Tags get the residual
        # (non-genre, non-system) list.
        item["Genres"] = genres
        item["Tags"] = residual

    if performers:
        people_list = []
        for p in performers:
            if p.get("name"):
                person = {
                    "Name": p.get("name"),
                    "Type": "Actor",
                    "Role": "",
                    "Id": f"person-{p.get('id')}",
                }
                if p.get("image_path"):
                    person_tag = f"p{p.get('id')}"
                    person["PrimaryImageTag"] = person_tag
                    person["ImageTags"] = {"Primary": person_tag}
                    person["ImageBlurHashes"] = {"Primary": {person_tag: "000000"}}
                people_list.append(person)
        item["People"] = people_list

    if path:
        item["Path"] = path
        item["LocationType"] = "FileSystem"

        file_data = files[0] if files else {}
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

        audio_stream_idx = 1
        effective_audio_codec = audio_codec if audio_codec else "aac"
        audio_stream = {
            "Index": audio_stream_idx,
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
        }
        media_streams.append(audio_stream)
        audio_stream_idx += 1

        captions = scene.get("captions") or []
        for idx, caption in enumerate(captions):
            lang_code = caption.get("language_code", "und")
            caption_type = (caption.get("caption_type", "") or "").lower()
            if caption_type not in ("srt", "vtt"):
                caption_type = "vtt"
            codec = "srt" if caption_type == "srt" else "webvtt"
            lang_names = {
                "en": "English", "de": "German", "es": "Spanish",
                "fr": "French", "it": "Italian", "nl": "Dutch",
                "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
                "zh": "Chinese", "ru": "Russian", "und": "Unknown"
            }
            display_lang = lang_names.get(lang_code, lang_code.upper())
            media_streams.append({
                "Index": audio_stream_idx + idx,
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
                "DeliveryUrl": f"Subtitles/{idx + 1}/0/Stream.{caption_type}"
            })

        item["HasSubtitles"] = len(captions) > 0

        media_source = {
            "Id": item_id,
            "Name": title,
            "Path": path,
            "Protocol": "File",
            "Type": "Default",
            "Container": container,
            "RunTimeTicks": int(duration * 10000000) if duration else 0,
            "Size": int(file_size) if file_size else 0,
            "Bitrate": bit_rate if bit_rate else 0,
            "SupportsDirectPlay": True,
            "SupportsDirectStream": True,
            "SupportsTranscoding": False,
            "MediaStreams": media_streams,
            "DefaultAudioStreamIndex": 1,
            "DefaultSubtitleStreamIndex": -1,
        }

        item["MediaSources"] = [media_source]
        # Web client's playbackManager reads MediaStreams / VideoType /
        # Container off the top-level item (not just the MediaSource)
        # when constructing the stream URL and selecting audio/sub tracks.
        item["MediaStreams"] = media_streams
        item["Container"] = container
        item["VideoType"] = "VideoFile"
        item["SourceType"] = "Default"
        item["PartCount"] = 1

    return item
