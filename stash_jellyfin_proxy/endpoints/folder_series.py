"""Folder-as-Series shape (issue #29, `folders_as_series` profiles).

SenPlayer only offers Next/Previous for Episodes: it opens a Series, loads
/Shows/{seriesId}/Episodes?SeasonId=..., and steps through that list. For
profiles with folders_as_series, browsable scene folders are presented as a
Series so playback started inside them gets a working queue:

    studio-N   → Series (scenes of studio N)

Seasons are calendar years of the scene date (``fseason-<folder>-<year>``,
year 0 = undated) so a studio with thousands of scenes isn't one giant list.
Episodes within a season are ordered by date ascending, then scene id.

Tags are left alone: TAG_GROUPS libraries (``tag-<slug>``) are top-level
views (a one-show version hid the all-scenes listing), and individual tags in
the Tags library (``tagitem-N``) were dropped too — no workable SenPlayer path.
"""
import logging
from typing import Dict, Optional, Tuple

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item
from stash_jellyfin_proxy.stash.client import stash_query

logger = logging.getLogger("stash-jellyfin-proxy")

FSEASON_PREFIX = "fseason-"
FOLDER_PREFIXES = ("studio-",)

_SCENE_FIELDS = (
    "id title code date details play_count resume_time last_played_at "
    "files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } "
    "studio { id name tags { name } parent_studio { id name tags { name } } } "
    "tags { name } performers { name id image_path } "
    "captions { language_code caption_type } "
    "stash_ids { stash_id }"
)


def is_folder_id(raw: str) -> bool:
    """True for studio-N with a numeric N."""
    for p in FOLDER_PREFIXES:
        if raw and raw.startswith(p) and raw[len(p):].isdigit():
            return True
    return False


def parse_fseason(raw: str) -> Optional[Tuple[str, int]]:
    """fseason-studio-12-2024 → ("studio-12", 2024); None if malformed."""
    if not raw or not raw.startswith(FSEASON_PREFIX):
        return None
    try:
        folder, year = raw[len(FSEASON_PREFIX):].rsplit("-", 1)
        year_n = int(year)
    except ValueError:
        return None
    if not is_folder_id(folder) or year_n < 0:
        return None
    return folder, year_n


def season_id(folder_id: str, year: int) -> str:
    return f"{FSEASON_PREFIX}{folder_id}-{year}"


def as_series_shape(item: dict) -> dict:
    """Retype a studio-N BoxSet item as a Series (in place)."""
    if item.get("Type") == "BoxSet" and is_folder_id(str(item.get("Id", ""))):
        item["Type"] = "Series"
        item.pop("CollectionType", None)
        if not item.get("BackdropImageTags"):
            item["BackdropImageTags"] = ["img"]
    return item


def _scene_filter(folder_id: str) -> Dict:
    return {"studios": {"value": [folder_id[len("studio-"):]], "modifier": "INCLUDES"}}


async def _folder_name(folder_id: str) -> str:
    q = """query FolderName($id: ID!) { findStudio(id: $id) { name } }"""
    res = await stash_query(q, {"id": folder_id[len("studio-"):]})
    obj = ((res or {}).get("data") or {}).get("findStudio") or {}
    return obj.get("name") or folder_id


def _year(scene: dict) -> int:
    d = scene.get("date") or ""
    return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else 0


def _order_key(scene: dict):
    return (scene.get("date") or "9999", int(scene["id"]) if str(scene.get("id", "")).isdigit() else 0)


async def _folder_scenes(folder_id: str, fields: str) -> list:
    q = f"""query FolderSeriesScenes($f: SceneFilterType) {{
        findScenes(scene_filter: $f, filter: {{per_page: -1, sort: "date", direction: ASC}}) {{
            scenes {{ {fields} }}
        }}
    }}"""
    res = await stash_query(q, {"f": _scene_filter(folder_id)})
    scenes = ((res or {}).get("data") or {}).get("findScenes", {}).get("scenes") or []
    return sorted(scenes, key=_order_key)


def _season_label(year: int) -> str:
    return str(year) if year else "Undated"


def _season_dto(folder_id: str, name: str, year: int, count: int) -> dict:
    sid = season_id(folder_id, year)
    return {
        "Name": _season_label(year),
        "SortName": f"{year:04d}",
        "Id": sid,
        "ServerId": runtime.SERVER_ID,
        "Type": "Season",
        "IsFolder": True,
        "ParentId": folder_id,
        "SeriesId": folder_id,
        "SeriesName": name,
        "IndexNumber": year,
        "ChildCount": count,
        "RecursiveItemCount": count,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
        "BackdropImageTags": ["img"],
        "UserData": {
            "PlaybackPositionTicks": 0, "PlayCount": 0,
            "IsFavorite": False, "Played": False, "Key": sid,
        },
    }


async def folder_series_seasons(folder_id: str) -> dict:
    """/Shows/{folder}/Seasons → one season per scene year."""
    if not is_folder_id(folder_id):
        return {"Items": [], "TotalRecordCount": 0}
    name = await _folder_name(folder_id)
    counts: Dict[int, int] = {}
    for s in await _folder_scenes(folder_id, "id date"):
        y = _year(s)
        counts[y] = counts.get(y, 0) + 1
    items = [_season_dto(folder_id, name, y, counts[y]) for y in sorted(counts)]
    return {"Items": items, "TotalRecordCount": len(items)}


async def folder_season_item(raw_season_id: str) -> dict:
    """Item detail for fseason-<folder>-<year> ({} if malformed/empty)."""
    parsed = parse_fseason(raw_season_id)
    if not parsed:
        return {}
    folder_id, year = parsed
    count = sum(1 for s in await _folder_scenes(folder_id, "id date") if _year(s) == year)
    if not count:
        return {}
    return _season_dto(folder_id, await _folder_name(folder_id), year, count)


async def folder_series_episodes(folder_id: str, want_season: Optional[str] = None) -> dict:
    """/Shows/{folder}/Episodes[?SeasonId=fseason-...] → scenes as ordered
    Episodes. Without a season, every scene in the folder is returned."""
    if not is_folder_id(folder_id):
        return {"Items": [], "TotalRecordCount": 0}
    want_year: Optional[int] = None
    if want_season:
        parsed = parse_fseason(want_season)
        if not parsed or parsed[0] != folder_id:
            return {"Items": [], "TotalRecordCount": 0}
        want_year = parsed[1]

    from stash_jellyfin_proxy.mapping.genre import genre_allowed_names
    await genre_allowed_names()
    name = await _folder_name(folder_id)
    scenes = await _folder_scenes(folder_id, _SCENE_FIELDS)

    items = []
    positions: Dict[int, int] = {}
    for s in scenes:
        y = _year(s)
        positions[y] = positions.get(y, 0) + 1
        if want_year is not None and y != want_year:
            continue
        sid = season_id(folder_id, y)
        it = format_jellyfin_item(s, parent_id=sid)
        it.update({
            "Type": "Episode",
            "SeriesId": folder_id,
            "SeriesName": name,
            "SeasonId": sid,
            "SeasonName": _season_label(y),
            "ParentIndexNumber": y,
            "IndexNumber": positions[y],
        })
        items.append(it)
    return {"Items": items, "TotalRecordCount": len(items)}
