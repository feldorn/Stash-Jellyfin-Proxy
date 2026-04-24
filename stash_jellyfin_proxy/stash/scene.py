"""Lightweight scene-info lookups used for logging, dashboard display,
and stream tracking. The full scene→Item mapping lives in
proxy/mapping/scene.py; this module only fetches the handful of fields
the RequestLoggingMiddleware and /api/streams endpoint need."""
from typing import Dict

from .client import stash_query


async def get_scene_info(scene_id: str) -> Dict:
    """Fetch title, performer(s), duration, and file size for a scene.
    Returns a dict with safe defaults if the query fails or the scene
    doesn't exist — callers rely on these keys always being present."""
    try:
        numeric_id = scene_id.replace("scene-", "")
        query = """query($id: ID!) {
            findScene(id: $id) {
                title
                files { basename duration size }
                performers { name }
            }
        }"""
        result = await stash_query(query, {"id": numeric_id})
        scene = result.get("data", {}).get("findScene")
        if scene:
            title = scene.get("title")
            duration = 0
            file_size = 0
            files = scene.get("files", [])
            if files:
                if not title and files[0].get("basename"):
                    title = files[0]["basename"]
                duration = files[0].get("duration", 0) or 0
                file_size = files[0].get("size", 0) or 0
            if not title:
                title = scene_id

            performers = scene.get("performers", [])
            performer = performers[0]["name"] if performers else ""
            if len(performers) > 1:
                performer = f"{performer} +{len(performers)-1}"

            return {"title": title, "performer": performer, "duration": duration, "file_size": file_size}
    except Exception:
        pass
    return {"title": scene_id, "performer": "", "duration": 0, "file_size": 0}


async def get_scene_title(scene_id: str) -> str:
    """Shortcut for log messages."""
    return (await get_scene_info(scene_id)).get("title", scene_id)
