"""Small Stash-query helper functions used by list and search endpoints.

The larger `transform_saved_filter_to_graphql` (240 lines) still lives in
the monolith until endpoint_items extracts — it's only called from there.
"""
import logging
from typing import Tuple

logger = logging.getLogger("stash-jellyfin-proxy")


def _default_sort_for_request(request, context: str, runtime) -> str:
    """Pick the configured per-library default SortBy value that best
    matches the request's ParentId. Falls back to old hardcoded defaults
    ("PremiereDate" / "SortName") when nothing maps."""
    pid = (request.query_params.get("ParentId") or
           request.query_params.get("parentId") or "")
    if pid.startswith("tag-") and pid not in ("tag-favorites", "tags-favorites", "tags-all"):
        return runtime.TAG_GROUPS_DEFAULT_SORT
    if pid.startswith("filter-"):
        return runtime.SAVED_FILTERS_DEFAULT_SORT
    if pid == "root-studios" or pid.startswith("studio-"):
        return runtime.STUDIOS_DEFAULT_SORT if context == "folders" else runtime.SCENES_DEFAULT_SORT
    if pid == "root-performers" or pid.startswith("performer-") or pid.startswith("person-"):
        return runtime.PERFORMERS_DEFAULT_SORT if context == "folders" else runtime.SCENES_DEFAULT_SORT
    if pid == "root-groups" or pid.startswith("group-"):
        return runtime.GROUPS_DEFAULT_SORT if context == "folders" else runtime.SCENES_DEFAULT_SORT
    if pid == "root-scenes" or pid.startswith("tagitem-"):
        return runtime.SCENES_DEFAULT_SORT
    # Fall back to the original hardcoded defaults.
    return "PremiereDate" if context == "scenes" else "SortName"


def get_stash_sort_params(request, context: str = "scenes") -> Tuple[str, str]:
    """Map Jellyfin SortBy/SortOrder to Stash sort/direction.
    context: 'scenes' for scene listings, 'folders' for
    performers/studios/groups/tags. When the request omits SortBy, fall
    back to the configured per-library default derived from the current
    ParentId (runtime.{SCENES,STUDIOS,PERFORMERS,GROUPS,TAG_GROUPS,
    SAVED_FILTERS}_DEFAULT_SORT)."""
    from stash_jellyfin_proxy import runtime  # local — runtime mutates at hot-reload
    sort_by_raw = (
        request.query_params.get("SortBy")
        or request.query_params.get("sortBy")
        or _default_sort_for_request(request, context, runtime)
    )
    sort_order = (
        request.query_params.get("SortOrder")
        or request.query_params.get("sortOrder")
        or ("Descending" if context == "scenes" else "Ascending")
    )

    sort_by = sort_by_raw.split(",")[0].strip()

    if context == "folders":
        sort_mapping = {
            "sortname": "name", "name": "name",
            "datecreated": "created_at", "premieredate": "created_at",
            "datelastcontentadded": "created_at",
            "random": "random", "communityrating": "rating",
        }
        default_sort = "name"
    else:
        sort_mapping = {
            "sortname": "title", "name": "title",
            "premieredate": "date",
            "datecreated": "created_at",
            "datelastcontentadded": "created_at",
            "dateplayed": "last_played_at",
            "productionyear": "date",
            "random": "random", "runtime": "duration",
            "communityrating": "rating", "playcount": "play_count",
            "criticrating": "rating",
            "resolution": "bitrate",
        }
        default_sort = "date"

    stash_sort = sort_mapping.get(sort_by.lower(), default_sort)
    stash_direction = "ASC" if sort_order == "Ascending" else "DESC"

    logger.debug(f"Sort mapping ({context}): {sort_by_raw} -> {sort_by} -> {stash_sort} {stash_direction}")
    return stash_sort, stash_direction


def scene_filter_clause_for_parent(parent_id):
    """Build the Stash GraphQL `scene_filter:` clause + variables dict for
    a proxy parent_id context. Returns (clause_string, vars_dict) or
    (None, None) if no applicable filter."""
    if not parent_id:
        return None, None
    if parent_id.startswith("performer-"):
        pid = parent_id.replace("performer-", "")
        return "scene_filter: {performers: {value: $ids, modifier: INCLUDES}}", {"ids": [pid]}
    if parent_id.startswith("studio-"):
        sid = parent_id.replace("studio-", "")
        return "scene_filter: {studios: {value: $ids, modifier: INCLUDES}}", {"ids": [sid]}
    if parent_id.startswith("group-"):
        gid = parent_id.replace("group-", "")
        return "scene_filter: {movies: {value: $ids, modifier: INCLUDES}}", {"ids": [gid]}
    if parent_id.startswith("tagitem-"):
        tid = parent_id.replace("tagitem-", "")
        return "scene_filter: {tags: {value: $ids, modifier: INCLUDES}}", {"ids": [tid]}
    return None, None
