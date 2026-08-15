"""Tests for mapping.scene.format_jellyfin_item — the central mapper.

Focused on issue #28 bug 3: GenreItems (NameGuidPair[]) must be emitted
alongside the legacy Genres (string[]) so newer Jellyfin SDK clients
(Yamby) render the detail-page genre row and can filter by genre.
"""
import pytest

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping import genre, scene as scene_mod
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item


@pytest.fixture(autouse=True)
def _reset_runtime():
    saved = (runtime.SERVER_ID, runtime.SERIES_TAG, runtime.FAVORITE_TAG,
             runtime.GENRE_PARENT_TAG, list(runtime.TAG_GROUPS))
    runtime.SERVER_ID = "test-server-id"
    runtime.SERIES_TAG = "Series"
    runtime.FAVORITE_TAG = "FAVORITE"
    runtime.GENRE_PARENT_TAG = "GENRE"
    runtime.TAG_GROUPS = []
    genre.invalidate_allowed_cache()
    yield
    (runtime.SERVER_ID, runtime.SERIES_TAG, runtime.FAVORITE_TAG,
     runtime.GENRE_PARENT_TAG, runtime.TAG_GROUPS) = saved
    genre.invalidate_allowed_cache()


def _scene(tags):
    """Minimal scene payload with a caller-supplied tag list.
    Tags shape mirrors what `tags { name id }` returns from Stash."""
    return {
        "id": "42",
        "title": "Test scene",
        "date": "2024-01-01",
        "files": [{"path": "/x/y.mp4", "duration": 100.0}],
        "tags": tags,
        "performers": [],
        "studio": None,
    }


def test_genre_items_emitted_with_genre_prefix_ids():
    """Every entry in `Genres` that has a matching tag id gets a
    GenreItems entry with the `genre-<id>` shape. That's the shape
    endpoint_genres emits, so a client tap round-trips correctly."""
    scene = _scene([
        {"name": "POV", "id": "10"},
        {"name": "Anal", "id": "17"},
        {"name": "Blowjob", "id": "42"},
    ])
    item = format_jellyfin_item(scene)

    # Legacy field still there and matches.
    assert set(item["Genres"]) == {"POV", "Anal", "Blowjob"}

    # GenreItems: NameGuidPair[] with genre-<id> shape.
    assert "GenreItems" in item
    by_name = {g["Name"]: g["Id"] for g in item["GenreItems"]}
    assert by_name == {
        "POV": "genre-10",
        "Anal": "genre-17",
        "Blowjob": "genre-42",
    }


def test_genre_items_skips_tags_missing_ids():
    """A scene whose tags come from a legacy query (no `id` field)
    still gets a valid Genres list, but GenreItems must not synthesize
    a bogus id — those entries are silently omitted."""
    scene = _scene([
        {"name": "POV", "id": "10"},   # has id
        {"name": "Anal"},               # missing id
    ])
    item = format_jellyfin_item(scene)
    assert "POV" in item["Genres"]
    assert "Anal" in item["Genres"]
    # Only the tag that had an id shows up in GenreItems.
    assert item["GenreItems"] == [{"Name": "POV", "Id": "genre-10"}]


def test_genre_items_absent_when_no_tags():
    """A scene with no tags at all shouldn't have GenreItems (or Genres);
    the whole block that computes them is gated on `if tags:`."""
    scene = _scene([])
    item = format_jellyfin_item(scene)
    assert "GenreItems" not in item
    assert "Genres" not in item


def test_genre_items_preserves_alphabetical_order_of_genres():
    """GenreItems follows the same order as Genres (alphabetical from
    compute_genres). Predictable ordering on the client detail page."""
    scene = _scene([
        {"name": "Zebra", "id": "1"},
        {"name": "Alpha", "id": "2"},
        {"name": "Middle", "id": "3"},
    ])
    item = format_jellyfin_item(scene)
    names = [g["Name"] for g in item["GenreItems"]]
    assert names == item["Genres"]  # same order


# --- issue #28 followup: video stream DisplayTitle ---
#
# Jellyfin SDK clients (Yamby) render the detail-page header video chip
# from MediaStreams[0].DisplayTitle. The audio stream already sets it;
# the video stream did not, so the chip rendered blank. The label follows
# the Jellyfin convention: `{resolution bucket} {CODEC}`.

def _scene_with_dims(width, height, codec="h264"):
    """Scene payload that exercises the video-stream construction path."""
    return {
        "id": "1",
        "title": "T",
        "files": [{
            "path": "/x.mp4",
            "duration": 100.0,
            "width": width,
            "height": height,
            "video_codec": codec,
            "audio_codec": "aac",
            "size": 0,
        }],
        "tags": [],
        "performers": [],
        "studio": None,
    }


def test_video_stream_display_title_1080p():
    item = format_jellyfin_item(_scene_with_dims(1920, 1080, "h264"))
    video = item["MediaStreams"][0]
    assert video["Type"] == "Video"
    assert video["DisplayTitle"] == "1080p H264"
    assert video["Title"] == "1080p H264"


def test_video_stream_display_title_4k():
    item = format_jellyfin_item(_scene_with_dims(3840, 2160, "hevc"))
    assert item["MediaStreams"][0]["DisplayTitle"] == "4K HEVC"


def test_video_stream_display_title_720p():
    item = format_jellyfin_item(_scene_with_dims(1280, 720, "h264"))
    assert item["MediaStreams"][0]["DisplayTitle"] == "720p H264"


def test_video_stream_display_title_sd_and_odd_heights():
    """480 → SD; smaller heights fall through to `{h}p`."""
    assert format_jellyfin_item(_scene_with_dims(720, 480, "mpeg4"))["MediaStreams"][0]["DisplayTitle"] == "SD MPEG4"
    assert format_jellyfin_item(_scene_with_dims(640, 360, "vp9"))["MediaStreams"][0]["DisplayTitle"] == "360p VP9"


def test_video_stream_display_title_absent_when_no_dimensions():
    """No width/height in the file record → we don't invent a resolution
    label. Chip renders blank rather than lie about the media."""
    scene = _scene_with_dims(0, 0)
    item = format_jellyfin_item(scene)
    video = item["MediaStreams"][0]
    assert "DisplayTitle" not in video
    assert "Title" not in video
