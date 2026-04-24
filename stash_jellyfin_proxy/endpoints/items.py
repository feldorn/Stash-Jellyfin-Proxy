"""Items listing, search, and single-item detail endpoints.

`endpoint_items` is the core browse/search handler — it dispatches based on
ParentId prefix, PersonIds, searchTerm, saved-filter ids, and Ids params.
`endpoint_item_details` handles single-item fetches by Jellyfin item id.
`transform_saved_filter_to_graphql` translates Stash's saved-filter JSON
format into the GraphQL input shape the API expects.

Helper functions:
  is_sort_only_filter       — skip filters that only set sort order
  stash_get_saved_filters   — fetch + cache saved filters from Stash
  format_filters_folder     — build the FILTERS virtual folder item
  format_saved_filter_item  — build a single saved-filter folder item
"""
import json
import logging
import os
import random
from typing import Any, Dict, List, Optional

from starlette.responses import JSONResponse

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.scene import format_jellyfin_item, is_group_favorite
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.tags import get_or_create_tag
from stash_jellyfin_proxy.stash.query_helpers import (
    get_stash_sort_params,
    scene_filter_clause_for_parent as _scene_filter_clause_for_parent,
)
from stash_jellyfin_proxy.endpoints.users import parse_emby_auth_header
from stash_jellyfin_proxy.util.ids import extract_numeric_id, get_numeric_id, make_guid
from stash_jellyfin_proxy.util.sort import sort_name_for

logger = logging.getLogger("stash-jellyfin-proxy")

def is_sort_only_filter(saved_filter: Dict[str, Any]) -> bool:
    """
    Check if a saved filter only defines sorting (no actual filter criteria).
    Sort-only filters are not useful in Infuse since we can't control sort order.
    Returns True if the filter has no meaningful filtering criteria.
    """
    # Get the object_filter (the actual filtering criteria)
    object_filter = saved_filter.get("object_filter")

    # Parse if string
    if isinstance(object_filter, str):
        try:
            object_filter = json.loads(object_filter)
        except:
            object_filter = {}

    # Null or empty object_filter means no filtering
    if not object_filter or object_filter == {}:
        # Check find_filter for search query
        find_filter = saved_filter.get("find_filter") or {}
        # If there's a search query (q), it's not sort-only
        if find_filter.get("q"):
            return False
        # Only has sort/direction or page/per_page - it's sort-only
        logger.debug(f"Filter '{saved_filter.get('name')}' is sort-only (empty object_filter, no search query)")
        return True

    # Check if object_filter only has empty values
    def has_meaningful_filter(obj):
        """Recursively check if object has any non-empty filter values."""
        if obj is None:
            return False
        if isinstance(obj, dict):
            for key, value in obj.items():
                # Skip pagination/sorting keys
                if key in ('page', 'per_page', 'sort', 'direction'):
                    continue
                if has_meaningful_filter(value):
                    return True
            return False
        if isinstance(obj, list):
            return len(obj) > 0 and any(has_meaningful_filter(v) for v in obj)
        if isinstance(obj, str):
            return len(obj) > 0
        if isinstance(obj, bool):
            return True  # Boolean criteria like "organized: true" is meaningful
        if isinstance(obj, (int, float)):
            return True  # Numeric criteria is meaningful
        return False

    if not has_meaningful_filter(object_filter):
        logger.debug(f"Filter '{saved_filter.get('name')}' is sort-only (no meaningful filter criteria)")
        return True

    return False


async def stash_get_saved_filters(mode: str, exclude_sort_only: bool = True) -> List[Dict[str, Any]]:
    """Get saved filters from Stash for a specific mode (SCENES, PERFORMERS, STUDIOS, GROUPS).

    Args:
        mode: Filter mode (SCENES, PERFORMERS, STUDIOS, GROUPS, TAGS)
        exclude_sort_only: If True, exclude filters that only define sorting
    """
    query = """query FindSavedFilters($mode: FilterMode) {
        findSavedFilters(mode: $mode) {
            id
            name
            mode
            find_filter { q page per_page sort direction }
            object_filter
            ui_options
        }
    }"""
    res = await stash_query(query, {"mode": mode})
    filters = res.get("data", {}).get("findSavedFilters", [])

    if exclude_sort_only:
        original_count = len(filters)
        filters = [f for f in filters if not is_sort_only_filter(f)]
        skipped = original_count - len(filters)
        if skipped > 0:
            logger.debug(f"Excluded {skipped} sort-only filters for mode {mode}")

    logger.debug(f"Found {len(filters)} saved filters for mode {mode}")
    return filters


FILTER_MODE_MAP = {
    "root-scenes": "SCENES",
    "root-performers": "PERFORMERS",
    "root-studios": "STUDIOS",
    "root-groups": "GROUPS",
    "root-tags": "TAGS",
}

async def format_filters_folder(parent_id: str) -> Dict[str, Any]:
    """Create a Jellyfin folder item for the FILTERS special folder."""
    filter_mode = FILTER_MODE_MAP.get(parent_id, "SCENES")
    filters_id = f"filters-{filter_mode.lower()}"

    # Get count of saved filters for this mode
    filters = await stash_get_saved_filters(filter_mode)
    filter_count = len(filters)

    return {
        "Name": "FILTERS",
        "SortName": "!!!FILTERS",  # Sort to top
        "Id": filters_id,
        "ServerId": runtime.SERVER_ID,
        "Type": "BoxSet",
        "IsFolder": True,
        "CollectionType": "movies",
        "ChildCount": filter_count,
        "RecursiveItemCount": filter_count,
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}},
        "PrimaryImageAspectRatio": 0.6667,
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": filters_id
        }
    }

def format_saved_filter_item(saved_filter: Dict[str, Any], parent_id: str) -> Dict[str, Any]:
    """Format a saved filter as a browsable folder item."""
    filter_id = saved_filter.get("id")
    filter_name = saved_filter.get("name", f"Filter {filter_id}")
    filter_mode = saved_filter.get("mode", "SCENES").lower()

    item_id = f"filter-{filter_mode}-{filter_id}"

    return {
        "Name": filter_name,
        "SortName": filter_name,
        "Id": item_id,
        "ServerId": runtime.SERVER_ID,
        "Type": "BoxSet",
        "IsFolder": True,
        "CollectionType": "movies",
        "ParentId": parent_id,
        "ImageTags": {"Primary": "img"},
        "ImageBlurHashes": {"Primary": {"img": "000000"}},
        "PrimaryImageAspectRatio": 0.6667,
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": item_id
        }
    }


def transform_saved_filter_to_graphql(object_filter, filter_mode="SCENES"):
    """
    Transform a saved filter's object_filter format to GraphQL query format.

    Saved filters use a complex format like:
        {'is_missing': {'modifier': 'EQUALS', 'value': 'cover'}}
        {'tags': {'value': ['123', '456'], 'modifier': 'INCLUDES'}}
        {'details': {'modifier': 'IS_NULL'}}  # No value for null checks
        {'duration': {'modifier': 'BETWEEN', 'value': 600, 'value2': 1800}}  # Range
        {'date': {'modifier': 'GREATER_THAN', 'value': '2023-01-01'}}  # Date comparison

    GraphQL expects:
        {'is_missing': 'cover'}
        {'tags': {'value': ['123', '456'], 'modifier': INCLUDES}}
        {'details': {'value': '', 'modifier': IS_NULL}}  # Empty string for null checks
        {'duration': {'value': 600, 'value2': 1800, 'modifier': BETWEEN}}  # Range preserved

    Supported modifiers:
        - EQUALS, NOT_EQUALS
        - INCLUDES, INCLUDES_ALL, EXCLUDES
        - IS_NULL, NOT_NULL
        - GREATER_THAN, LESS_THAN
        - BETWEEN (with value and value2)
        - MATCHES_REGEX

    Supported field types:
        - String fields: title, path, details, url, code, director, phash
        - Boolean fields: organized, interactive, performer_favorite, has_markers
        - Integer fields: rating100, o_counter, play_count, file_count
        - Duration fields: duration (in seconds), resume_time
        - Date fields: date, created_at, updated_at
        - Resolution fields: resolution (enum: VERY_LOW, LOW, R360P, R480P, R720P, R1080P, R1440P, FOUR_K, FIVE_K, etc.)
        - Hierarchical fields: tags, performers, studios, movies/groups
    """
    if not object_filter or not isinstance(object_filter, dict):
        return {}

    result = {}

    # Fields that should be passed as simple booleans (not wrapped in modifier structure)
    BOOLEAN_FIELDS = {'organized', 'interactive', 'performer_favorite', 'has_markers',
                      'ignore_auto_tag', 'favorite', 'is_missing'}

    # Fields that use IntCriterionInput (value/value2/modifier structure)
    INT_CRITERION_FIELDS = {'rating100', 'o_counter', 'play_count', 'file_count',
                            'width', 'height', 'framerate', 'bitrate', 'duration',
                            'resume_time', 'tag_count', 'performer_count', 'scene_count',
                            'gallery_count', 'marker_count', 'image_count'}

    # Fields that use date comparison
    DATE_FIELDS = {'date', 'created_at', 'updated_at', 'last_played_at', 'birthdate', 'death_date'}

    # Fields that use HierarchicalMultiCriterionInput
    HIERARCHICAL_FIELDS = {'tags', 'performers', 'studios', 'movies', 'groups', 'performer_tags'}

    # Fields that use MultiCriterionInput (IDs with modifier)
    MULTI_CRITERION_FIELDS = {'galleries', 'scenes', 'parents', 'children'}

    for key, value in object_filter.items():
        if value is None:
            continue

        # Handle nested filter groups (AND, OR, NOT)
        if key in ('AND', 'OR', 'NOT'):
            if isinstance(value, list):
                transformed = [transform_saved_filter_to_graphql(v, filter_mode) for v in value]
                # Filter out empty dicts from the list
                transformed = [t for t in transformed if t]
                if transformed:
                    result[key] = transformed
            elif isinstance(value, dict):
                transformed = transform_saved_filter_to_graphql(value, filter_mode)
                if transformed:
                    result[key] = transformed
            continue

        # Handle simple string fields that don't need transformation
        if isinstance(value, str):
            result[key] = value
            continue

        # Handle boolean fields
        if isinstance(value, bool):
            result[key] = value
            continue

        # Handle integer fields
        if isinstance(value, (int, float)):
            result[key] = value
            continue

        # Handle list of simple values
        if isinstance(value, list):
            result[key] = value
            continue

        # Handle dict with modifier/value structure
        if isinstance(value, dict):
            modifier = value.get('modifier')
            val = value.get('value')
            val2 = value.get('value2')  # For BETWEEN modifier

            # Special case: is_missing just needs the string value
            if key == 'is_missing' and modifier == 'EQUALS':
                result[key] = val
                continue

            # Handle IS_NULL and NOT_NULL modifiers - they need an empty string value
            if modifier in ('IS_NULL', 'NOT_NULL'):
                result[key] = {'value': '', 'modifier': modifier}
                continue

            # Handle BETWEEN modifier (ranges) - preserve value2
            if modifier == 'BETWEEN':
                if val is not None and val2 is not None:
                    # Ensure numeric values are properly typed
                    try:
                        if key in INT_CRITERION_FIELDS or key in DATE_FIELDS:
                            if key in DATE_FIELDS:
                                # Keep dates as strings
                                result[key] = {'value': val, 'value2': val2, 'modifier': modifier}
                            else:
                                result[key] = {'value': int(val) if not isinstance(val, int) else val,
                                             'value2': int(val2) if not isinstance(val2, int) else val2,
                                             'modifier': modifier}
                        else:
                            result[key] = {'value': val, 'value2': val2, 'modifier': modifier}
                    except (ValueError, TypeError):
                        result[key] = {'value': val, 'value2': val2, 'modifier': modifier}
                    continue

            # Handle comparison modifiers (GREATER_THAN, LESS_THAN)
            if modifier in ('GREATER_THAN', 'LESS_THAN', 'EQUALS', 'NOT_EQUALS'):
                if val is not None:
                    # Handle nested value objects like {'value': 1} -> 1
                    if isinstance(val, dict) and 'value' in val and len(val) == 1:
                        val = val['value']

                    # Convert string booleans to actual booleans
                    if isinstance(val, str):
                        if val.lower() == 'true':
                            val = True
                        elif val.lower() == 'false':
                            val = False

                    # For simple boolean fields with EQUALS modifier, pass boolean directly
                    if key in BOOLEAN_FIELDS and isinstance(val, bool) and modifier == 'EQUALS':
                        result[key] = val
                        continue

                    # For integer fields, ensure proper typing
                    if key in INT_CRITERION_FIELDS and not isinstance(val, bool):
                        try:
                            val = int(val) if isinstance(val, str) else val
                        except (ValueError, TypeError):
                            pass

                    result[key] = {'value': val, 'modifier': modifier}
                    continue

            # For most filter fields with modifier/value, pass through as-is
            if modifier and val is not None:
                # Handle nested value objects like {'value': 1} -> 1
                if isinstance(val, dict) and 'value' in val and len(val) == 1:
                    val = val['value']

                # Convert string booleans to actual booleans
                if isinstance(val, str):
                    if val.lower() == 'true':
                        val = True
                    elif val.lower() == 'false':
                        val = False

                # For simple boolean fields with EQUALS modifier, just pass the boolean directly
                if key in BOOLEAN_FIELDS and isinstance(val, bool) and modifier == 'EQUALS':
                    result[key] = val
                    continue

                # Handle HierarchicalMultiCriterionInput (tags, performers, studios, etc.)
                # Structure: {'items': [{'id': '123', 'label': 'Name'}], 'depth': 0, 'excluded': []}
                # Needs to become: {'value': ['123'], 'modifier': 'INCLUDES_ALL', 'depth': 0, 'excludes': []}
                if key in HIERARCHICAL_FIELDS and isinstance(val, dict) and 'items' in val:
                    items = val.get('items', [])
                    # Extract IDs from items
                    ids = [item.get('id') for item in items if item.get('id')]
                    depth = val.get('depth', 0)
                    # Note: Stash uses 'excluded' but GraphQL expects 'excludes'
                    excludes = val.get('excluded', [])
                    if isinstance(excludes, list):
                        # Extract IDs if excludes contains objects
                        excludes = [e.get('id') if isinstance(e, dict) else e for e in excludes]
                    result[key] = {'value': ids, 'modifier': modifier, 'depth': depth, 'excludes': excludes}
                    continue

                # Handle MultiCriterionInput (just IDs with modifier)
                if key in MULTI_CRITERION_FIELDS and isinstance(val, list):
                    # Extract IDs if val contains objects
                    ids = [v.get('id') if isinstance(v, dict) else v for v in val]
                    result[key] = {'value': ids, 'modifier': modifier}
                    continue

                # Handle resolution (enum type)
                if key == 'resolution':
                    result[key] = {'value': val, 'modifier': modifier}
                    continue

                # Handle orientation/aspect_ratio (enum types)
                if key in ('orientation', 'aspect_ratio'):
                    result[key] = {'value': val, 'modifier': modifier}
                    continue

                # Handle stash_id (with endpoint)
                if key == 'stash_id' and isinstance(val, dict):
                    result[key] = val
                    continue

                # Handle phash_distance (IntCriterionInput with distance field)
                if key == 'phash_distance' and isinstance(val, dict):
                    result[key] = val
                    continue

                result[key] = {'value': val, 'modifier': modifier}
                continue

            # For nested objects without modifier/value, recurse
            if not modifier:
                transformed = transform_saved_filter_to_graphql(value, filter_mode)
                if transformed:
                    result[key] = transformed
                continue

            # If we have modifier but no value, add empty string for value
            # (needed for some modifiers like IS_NULL, NOT_NULL)
            transformed = {'modifier': modifier, 'value': val if val is not None else ''}
            for k, v in value.items():
                if k not in ('modifier', 'value'):
                    transformed[k] = v
            result[key] = transformed

    return result


def _parse_filter_params(request):
    """Extract the multi-value filter params Jellyfin Web / Swiftfin send
    on a filtered scene list. Returns (genres, tags, years) — each a
    de-duplicated list."""
    qp = request.query_params

    def _multi(*keys):
        out = []
        for k in keys:
            # Repeated param (?genres=A&genres=B)
            for v in qp.getlist(k) if hasattr(qp, "getlist") else qp.multi_items():
                if isinstance(v, tuple):
                    k2, val = v
                    if k2 == k:
                        out.extend(s.strip() for s in (val or "").split(",") if s.strip())
                else:
                    out.extend(s.strip() for s in (v or "").split(",") if s.strip())
        return list(dict.fromkeys(out))

    genres = _multi("Genres", "genres")
    tags = _multi("Tags", "tags")
    years = _multi("Years", "years")
    return genres, tags, years


async def _resolve_tag_ids(tag_names):
    """Resolve tag names to Stash tag ids. Missing names are dropped
    silently. Results are batched into one GraphQL call per distinct
    name (Stash's name filter doesn't accept a list)."""
    ids = []
    for name in tag_names:
        try:
            res = await stash_query(
                """query FindTag($n: String!) {
                    findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}, filter: {per_page: 5}) {
                        tags { id name }
                    }
                }""",
                {"n": name},
            )
            tags = ((res or {}).get("data") or {}).get("findTags", {}).get("tags") or []
            match = next(
                (t for t in tags if (t.get("name") or "").lower() == name.lower()),
                None,
            )
            if match and match.get("id"):
                ids.append(match["id"])
        except Exception as e:
            logger.debug(f"tag lookup failed for '{name}': {e}")
    return ids


async def _filter_clause(request, filter_favorites: bool):
    """Build the extra scene_filter: {...} body (as a string) plus its
    variables dict for genre / tag / year filter params on the request.

    Returns (clause_parts, vars_dict). clause_parts is a list of
    "key: {...}" strings; callers stitch them together inside the
    scene_filter block. Empty list means no extra filter applies.

    Respects runtime.GENRE_FILTER_LOGIC (AND→INCLUDES_ALL, OR→INCLUDES)
    and runtime.FILTER_TAGS_WALK_HIERARCHY (depth: -1 when true).
    Years filter currently applies single-year; multi-year OR is a
    future enhancement."""
    genres, tags, years = _parse_filter_params(request)
    all_names = list(dict.fromkeys(genres + tags))

    parts = []
    vars_ = {}

    if all_names:
        tag_ids = await _resolve_tag_ids(all_names)
        if tag_ids:
            modifier = "INCLUDES_ALL" if (runtime.GENRE_FILTER_LOGIC or "AND").upper() == "AND" else "INCLUDES"
            depth_line = ", depth: -1" if runtime.FILTER_TAGS_WALK_HIERARCHY else ""
            parts.append(f"tags: {{value: $_filter_tag_ids, modifier: {modifier}{depth_line}}}")
            vars_["_filter_tag_ids"] = tag_ids

    if years:
        # Take the first parseable year; Swiftfin / Infuse send a single
        # value here, multi-year OR is outside Stash's scene_filter scope.
        for y in years:
            try:
                yr = int(y)
                parts.append(
                    f'date: {{value: "{yr}-01-01", value2: "{yr}-12-31", modifier: BETWEEN}}'
                )
                break
            except ValueError:
                continue

    return parts, vars_


async def _hero_pool(scene_fields: str, tag_ids_override: list) -> list:
    """Return the candidate scene pool for the Home-tab hero banner.

    Switches on `runtime.HERO_SOURCE`:
        recent            newest by created_at (default)
        random            uniform random across the library
        favorites         scenes tagged FAVORITE_TAG
        top_rated         rating100 >= HERO_MIN_RATING, sorted by rating desc
        recently_watched  last_played_at within 30 days

    When the legacy BANNER_MODE=tag is active and BANNER_TAGS resolved,
    the tag filter takes precedence over HERO_SOURCE.
    """
    pool_size = runtime.BANNER_POOL_SIZE

    # Legacy BANNER_MODE=tag path — explicit tag override wins.
    if runtime.BANNER_MODE == "tag" and tag_ids_override:
        q = f"""query HeroByTags($tids: [ID!], $per_page: Int!) {{
            findScenes(
                scene_filter: {{tags: {{value: $tids, modifier: INCLUDES}}}},
                filter: {{page: 1, per_page: $per_page, sort: "created_at", direction: DESC}}
            ) {{ scenes {{ {scene_fields} }} }}
        }}"""
        res = await stash_query(q, {"tids": tag_ids_override, "per_page": pool_size})
        pool = res.get("data", {}).get("findScenes", {}).get("scenes", []) or []
        logger.debug(f"Hero (banner_mode=tag): pool={len(pool)}")
        return pool

    source = (runtime.HERO_SOURCE or "recent").lower()

    if source == "random":
        q = f"""query HeroRandom($n: Int!) {{
            findScenes(filter: {{page: 1, per_page: $n, sort: "random", direction: DESC}}) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = await stash_query(q, {"n": pool_size})

    elif source == "favorites":
        fav_tag_id = None
        if runtime.FAVORITE_TAG:
            fav_tag_id = await get_or_create_tag(runtime.FAVORITE_TAG)
        if not fav_tag_id:
            logger.debug("hero_source=favorites but FAVORITE_TAG unresolved; falling back to recent")
            source = "recent"
        else:
            q = f"""query HeroFavorites($tid: [ID!], $n: Int!) {{
                findScenes(
                    scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                    filter: {{page: 1, per_page: $n, sort: "random", direction: DESC}}
                ) {{ scenes {{ {scene_fields} }} }}
            }}"""
            res = await stash_query(q, {"tid": [fav_tag_id], "n": pool_size})

    elif source == "top_rated":
        min_rating = int(runtime.HERO_MIN_RATING or 75)
        q = f"""query HeroTopRated($min: Int!, $n: Int!) {{
            findScenes(
                scene_filter: {{rating100: {{value: $min, modifier: GREATER_THAN_OR_EQUAL}}}},
                filter: {{page: 1, per_page: $n, sort: "rating", direction: DESC}}
            ) {{ scenes {{ {scene_fields} }} }}
        }}"""
        res = await stash_query(q, {"min": min_rating, "n": pool_size})

    elif source == "recently_watched":
        q = f"""query HeroRecent($n: Int!) {{
            findScenes(
                scene_filter: {{last_played_at: {{value: "1 month", modifier: LESS_THAN}}}},
                filter: {{page: 1, per_page: $n, sort: "last_played_at", direction: DESC}}
            ) {{ scenes {{ {scene_fields} }} }}
        }}"""
        res = await stash_query(q, {"n": pool_size})

    else:
        source = "recent"

    if source == "recent":
        q = f"""query HeroRecent($n: Int!) {{
            findScenes(filter: {{page: 1, per_page: $n, sort: "created_at", direction: DESC}}) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = await stash_query(q, {"n": pool_size})

    pool = ((res or {}).get("data") or {}).get("findScenes", {}).get("scenes", []) or []
    logger.debug(f"Hero (source={runtime.HERO_SOURCE}): pool={len(pool)}")
    return pool


async def endpoint_items(request):
    # Refresh the genre allow-list snapshot — format_jellyfin_item reads
    # it sync on each scene. Cached 5 min in mapping.genre so this is a
    # dict lookup on the hot path.
    from stash_jellyfin_proxy.mapping.genre import genre_allowed_names
    await genre_allowed_names()

    user_id = request.path_params.get("user_id")
    # Handle both ParentId and parentId (Infuse uses lowercase)
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    ids = request.query_params.get("Ids") or request.query_params.get("ids")

    # Pagination parameters with validation
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or runtime.DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, runtime.MAX_PAGE_SIZE))  # Enforce min=1, max=runtime.MAX_PAGE_SIZE

    # Sort parameters
    sort_field, sort_direction = get_stash_sort_params(request)

    # Check for PersonIds parameter (Infuse uses this when clicking on a person)
    person_ids = request.query_params.get("PersonIds") or request.query_params.get("personIds")

    # Check for searchTerm parameter (Infuse search functionality)
    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm")

    # Check for Filters parameter (e.g. Filters=IsFavorite used by SenPlayer/Swiftfin Favorites tab)
    filters_param = request.query_params.get("Filters") or request.query_params.get("filters") or ""
    filter_favorites = "isfavorite" in filters_param.lower()

    # Check includeItemTypes - handle both repeated params (includeItemTypes=Movie&includeItemTypes=Series)
    # and comma-separated values (includeItemTypes=Movie,Series)
    raw_type_list = [v for k, v in request.query_params.multi_items() if k.lower() == "includeitemtypes"]
    include_type_list = []
    for val in raw_type_list:
        include_type_list.extend([t.strip() for t in val.split(",") if t.strip()])
    include_types_lower = [t.lower() for t in include_type_list]
    has_movie_type = not include_type_list or "movie" in include_types_lower or "video" in include_types_lower
    restrict_to_movies = has_movie_type and "folder" not in include_types_lower and len(include_type_list) > 0

    # Debug: Log ALL query params (show multi-values properly)
    logger.debug(f"Items endpoint - ALL PARAMS: {dict(request.query_params)}, includeItemTypes={include_type_list}")
    logger.debug(f"Items endpoint - ParentId: {parent_id}, Ids: {ids}, PersonIds: {person_ids}, SearchTerm: {search_term}, StartIndex: {start_index}, Limit: {limit}, Sort: {sort_field} {sort_direction}")

    items = []
    total_count = 0

    # Full scene fields for queries (include performer image_path for People images, captions for subtitles)
    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { id name tags { name } parent_studio { id name tags { name } } } tags { name } performers { name id image_path } captions { language_code caption_type } stash_ids { stash_id }"

    if ids:
        # Specific items requested
        id_list = ids.split(',')
        for iid in id_list:
            q = f"""query FindScene($id: ID!) {{ findScene(id: $id) {{ {scene_fields} }} }}"""
            res = await stash_query(q, {"id": iid})
            scene = res.get("data", {}).get("findScene")
            if scene:
                items.append(format_jellyfin_item(scene))
        total_count = len(items)

    elif person_ids:
        # Infuse uses PersonIds parameter to filter by person/performer
        # Extract the numeric ID from person-123 or just 123 format
        person_id = person_ids.split(',')[0]  # Take first if multiple
        if person_id.startswith("person-"):
            performer_id = person_id.replace("person-", "")
        elif person_id.startswith("performer-"):
            performer_id = person_id.replace("performer-", "")
        else:
            performer_id = person_id

        logger.debug(f"PersonIds filter: fetching scenes for performer {performer_id}")

        # Get count for this performer
        count_q = """query CountScenes($pid: [ID!]) {
            findScenes(scene_filter: {performers: {value: $pid, modifier: INCLUDES}}) { count }
        }"""
        count_res = await stash_query(count_q, {"pid": [performer_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($pid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{performers: {{value: $pid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = await stash_query(q, {"pid": [performer_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"PersonIds filter: returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=f"person-{performer_id}"))

    elif search_term:
        # Handle search from Infuse/Swiftfin - query Stash with the search term
        # Strip any quotes that client might add around the search term
        clean_search = search_term.strip('"\'')

        logger.info(f"Search: '{clean_search}' (types={include_type_list})")

        # Only search for scenes if Movie/Video type is requested (or no type filter)
        # Skip for Series-only or Episode-only requests since Stash only has movie-type content.
        # Also honour runtime.SEARCH_INCLUDE_SCENES — users can disable scene
        # hits globally even when the client requests them.
        if not has_movie_type:
            logger.debug(f"Search skipped - requested types {include_type_list} don't include Movie/Video")
        elif not runtime.SEARCH_INCLUDE_SCENES:
            logger.debug("Search scenes skipped (search_include_scenes=false)")
        else:
            # Get count of matching scenes
            count_q = """query CountScenes($q: String!) {
                findScenes(filter: {q: $q}) { count }
            }"""
            count_res = await stash_query(count_q, {"q": clean_search})
            total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

            # Calculate page
            page = (start_index // limit) + 1

            # Query Stash with the search term
            q = f"""query FindScenes($q: String!, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                findScenes(filter: {{q: $q, page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                    scenes {{ {scene_fields} }}
                }}
            }}"""
            res = await stash_query(q, {"q": clean_search, "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            logger.debug(f"Search '{clean_search}' returned {len(scenes)} scenes (page {page}, total {total_count})")
            for s in scenes:
                items.append(format_jellyfin_item(s))

    elif parent_id and parent_id.startswith("filters-"):
        # List saved filters for a specific mode (filters-scenes, filters-performers, etc.)
        filter_mode = parent_id.replace("filters-", "").upper()
        saved_filters = await stash_get_saved_filters(filter_mode)
        total_count = len(saved_filters)

        logger.debug(f"Listing {total_count} saved filters for mode {filter_mode}")

        for sf in saved_filters:
            items.append(format_saved_filter_item(sf, parent_id))

    elif parent_id and parent_id.startswith("filter-"):
        # Apply a saved filter and show results
        # Format: filter-{mode}-{filter_id}
        parts = parent_id.split("-", 2)  # ['filter', 'scenes', '123']
        if len(parts) == 3:
            filter_mode = parts[1].upper()
            filter_id = parts[2]

            # Get the saved filter details
            query = """query FindSavedFilter($id: ID!) {
                findSavedFilter(id: $id) {
                    id name mode
                    find_filter { q page per_page sort direction }
                    object_filter
                }
            }"""
            res = await stash_query(query, {"id": filter_id})
            saved_filter = res.get("data", {}).get("findSavedFilter")

            if saved_filter:
                find_filter = saved_filter.get("find_filter") or {}
                object_filter = saved_filter.get("object_filter")

                # Parse object_filter if it's a string (JSON)
                import json
                if isinstance(object_filter, str):
                    try:
                        object_filter = json.loads(object_filter)
                    except Exception as e:
                        logger.warning(f"Failed to parse object_filter JSON: {e}")
                        object_filter = {}

                # Ensure object_filter is a dict, default to empty
                if object_filter is None:
                    object_filter = {}

                logger.debug(f"Applying saved filter '{saved_filter.get('name')}' (id={filter_id}, mode={filter_mode})")
                logger.debug(f"Raw object_filter type: {type(object_filter)}, value: {object_filter}")

                # Transform saved filter format to GraphQL query format
                graphql_filter = transform_saved_filter_to_graphql(object_filter, filter_mode)
                logger.debug(f"Transformed filter: {graphql_filter}")

                # Try querying Stash directly with the filter to see what happens
                # Also log the full saved filter data for debugging
                logger.debug(f"Full saved filter data: {saved_filter}")

                logger.debug(f"Filter find_filter: {find_filter}")
                logger.debug(f"Filter object_filter: {object_filter}")

                # Calculate page and sort
                page = (start_index // limit) + 1
                folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

                # Build the query with the saved filter's criteria
                # Each mode has its own filter type in Stash GraphQL
                if filter_mode == "SCENES":
                    # First get count with filter
                    count_q = """query CountScenes($scene_filter: SceneFilterType) {
                        findScenes(scene_filter: $scene_filter) { count }
                    }"""
                    logger.debug(f"Running count query with scene_filter: {graphql_filter}")
                    count_res = await stash_query(count_q, {"scene_filter": graphql_filter})
                    logger.debug(f"Count query response: {count_res}")
                    total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

                    # Get paginated results
                    q = f"""query FindScenes($scene_filter: SceneFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                        findScenes(
                            scene_filter: $scene_filter,
                            filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                        ) {{
                            scenes {{ {scene_fields} }}
                        }}
                    }}"""
                    res = await stash_query(q, {
                        "scene_filter": graphql_filter,
                        "page": page,
                        "per_page": limit,
                        "sort": sort_field,
                        "direction": sort_direction
                    })
                    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Saved filter returned {len(scenes)} scenes (page {page}, total {total_count})")
                    for s in scenes:
                        items.append(format_jellyfin_item(s, parent_id=parent_id))

                elif filter_mode == "PERFORMERS":
                    # Count performers with filter
                    count_q = """query CountPerformers($performer_filter: PerformerFilterType) {
                        findPerformers(performer_filter: $performer_filter) { count }
                    }"""
                    count_res = await stash_query(count_q, {"performer_filter": graphql_filter})
                    total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

                    # Get paginated performers
                    q = """query FindPerformers($performer_filter: PerformerFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findPerformers(
                            performer_filter: $performer_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            performers { id name image_path scene_count favorite }
                        }
                    }"""
                    res = await stash_query(q, {"performer_filter": graphql_filter, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])
                    logger.debug(f"Saved filter returned {len(performers)} performers (page {page}, total {total_count})")
                    for p in performers:
                        performer_item = {
                            "Name": p["name"],
                            "Id": f"performer-{p['id']}",
                            "ServerId": runtime.SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": p.get("scene_count", 0),
                            "RecursiveItemCount": p.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": bool(p.get("favorite")), "Played": False, "Key": f"performer-{p['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(performer_item)

                elif filter_mode == "STUDIOS":
                    # Count studios with filter
                    count_q = """query CountStudios($studio_filter: StudioFilterType) {
                        findStudios(studio_filter: $studio_filter) { count }
                    }"""
                    count_res = await stash_query(count_q, {"studio_filter": graphql_filter})
                    total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

                    # Get paginated studios
                    q = """query FindStudios($studio_filter: StudioFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findStudios(
                            studio_filter: $studio_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            studios { id name image_path scene_count }
                        }
                    }"""
                    res = await stash_query(q, {"studio_filter": graphql_filter, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
                    logger.debug(f"Saved filter returned {len(studios)} studios (page {page}, total {total_count})")
                    for s in studios:
                        studio_item = {
                            "Name": s["name"],
                            "Id": f"studio-{s['id']}",
                            "ServerId": runtime.SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": s.get("scene_count", 0),
                            "RecursiveItemCount": s.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(studio_item)

                elif filter_mode == "GROUPS":
                    # Count groups/movies with filter
                    count_q = """query CountGroups($group_filter: GroupFilterType) {
                        findGroups(group_filter: $group_filter) { count }
                    }"""
                    count_res = await stash_query(count_q, {"group_filter": graphql_filter})
                    total_count = count_res.get("data", {}).get("findGroups", {}).get("count", 0)

                    # Get paginated groups
                    q = """query FindGroups($group_filter: GroupFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findGroups(
                            group_filter: $group_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            groups { id name scene_count }
                        }
                    }"""
                    res = await stash_query(q, {"group_filter": graphql_filter, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                    groups = res.get("data", {}).get("findGroups", {}).get("groups", [])
                    logger.debug(f"Saved filter returned {len(groups)} groups (page {page}, total {total_count})")
                    for g in groups:
                        group_item = {
                            "Name": g["name"],
                            "Id": f"group-{g['id']}",
                            "ServerId": runtime.SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": g.get("scene_count", 0),
                            "RecursiveItemCount": g.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"group-{g['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(group_item)

                elif filter_mode == "TAGS":
                    # Use fixed page size for Stash queries to avoid pagination misalignment
                    # when Infuse changes limit between requests (e.g., 50 then 31)
                    # Stash pagination: items start at (page-1) * per_page
                    # If we use varying per_page, the offsets won't align with startIndex
                    STASH_PAGE_SIZE = 50  # Fixed internal page size

                    # Calculate which Stash page contains start_index
                    stash_page = (start_index // STASH_PAGE_SIZE) + 1
                    # Offset within that page
                    offset_in_page = start_index % STASH_PAGE_SIZE

                    logger.debug(f"TAGS filter pagination: startIndex={start_index}, limit={limit}, stash_page={stash_page}, offset_in_page={offset_in_page}")

                    q = """query FindTags($tag_filter: TagFilterType, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                        findTags(
                            tag_filter: $tag_filter,
                            filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                        ) {
                            count
                            tags { id name scene_count image_path favorite }
                        }
                    }"""
                    res = await stash_query(q, {"tag_filter": graphql_filter, "page": stash_page, "per_page": STASH_PAGE_SIZE, "sort": folder_sort, "direction": folder_dir})
                    data = res.get("data", {}).get("findTags", {})
                    total_count = data.get("count", 0)
                    all_tags = data.get("tags", [])

                    # Slice from offset_in_page, up to limit items
                    tags = all_tags[offset_in_page:offset_in_page + limit]

                    # If we need more items than remaining in this page, fetch next page too
                    while len(tags) < limit and (stash_page * STASH_PAGE_SIZE) < total_count:
                        stash_page += 1
                        res = await stash_query(q, {"tag_filter": graphql_filter, "page": stash_page, "per_page": STASH_PAGE_SIZE, "sort": folder_sort, "direction": folder_dir})
                        next_tags = res.get("data", {}).get("findTags", {}).get("tags", [])
                        tags.extend(next_tags[:limit - len(tags)])

                    # Log first and last 3 tag IDs to help identify duplicates/overlaps
                    first_ids = [t.get("id") for t in tags[:3]] if tags else []
                    last_ids = [t.get("id") for t in tags[-3:]] if len(tags) > 3 else first_ids
                    logger.debug(f"TAGS filter: returning {len(tags)} tags (total {total_count}), first IDs: {first_ids}, last IDs: {last_ids}")
                    for t in tags:
                        tag_item = {
                            "Name": t["name"],
                            "Id": f"tagitem-{t['id']}",
                            "ServerId": runtime.SERVER_ID,
                            "Type": "BoxSet",
                            "IsFolder": True,
                            "CollectionType": "movies",
                            "ChildCount": t.get("scene_count", 0),
                            "RecursiveItemCount": t.get("scene_count", 0),
                            "ParentId": parent_id,
                            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": t.get("favorite", False), "Played": False, "Key": f"tagitem-{t['id']}"},
                            "ImageTags": {"Primary": "img"},
                            "ImageBlurHashes": {"Primary": {"img": "000000"}},
                            "PrimaryImageAspectRatio": 0.6667,
                            "BackdropImageTags": []
                        }
                        items.append(tag_item)

                else:
                    logger.warning(f"Unsupported filter mode: {filter_mode}")
            else:
                logger.warning(f"Saved filter not found: {filter_id}")

    elif parent_id == "root-series":
        # Series library root → list of SERIES-tagged studios, typed as Series.
        series_tag_id = await get_or_create_tag(runtime.SERIES_TAG) if runtime.SERIES_TAG else None
        if not series_tag_id:
            logger.debug("root-series requested but SERIES_TAG not resolvable; returning empty")
            total_count = 0
        else:
            count_q = """query CountSeries($tid: [ID!]) {
                findStudios(studio_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
            }"""
            count_res = await stash_query(count_q, {"tid": [series_tag_id]})
            total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

            page = (start_index // limit) + 1
            q = """query FindSeriesStudios($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findStudios(
                    studio_filter: {tags: {value: $tid, modifier: INCLUDES}},
                    filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                ) {
                    studios { id name image_path scene_count }
                }
            }"""
            folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
            res = await stash_query(q, {"tid": [series_tag_id], "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
            for s in studios:
                items.append({
                    "Name": s["name"],
                    "Id": f"series-{s['id']}",
                    "ServerId": runtime.SERVER_ID,
                    "Type": "Series",
                    "IsFolder": True,
                    "ParentId": parent_id,
                    "ChildCount": s.get("scene_count", 0),
                    "RecursiveItemCount": s.get("scene_count", 0),
                    "PrimaryImageAspectRatio": 0.6667,
                    "ImageTags": {"Primary": "img"},
                    "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
                    "BackdropImageTags": ["img"],
                    "UserData": {
                        "PlaybackPositionTicks": 0, "PlayCount": 0,
                        "IsFavorite": False, "Played": False,
                        "Key": f"series-{s['id']}",
                    },
                })

    elif parent_id and parent_id.startswith("series-"):
        # Series detail. If the caller asks specifically for Episode types
        # (Swiftfin does this to build the series overview), return all
        # the series' episodes. Otherwise return the synthetic Seasons.
        studio_id = parent_id.replace("series-", "")
        want_episodes = "episode" in include_types_lower and "season" not in include_types_lower

        if want_episodes:
            q = f"""query FindSeriesEps($sid: [ID!]) {{
                findScenes(
                    scene_filter: {{studios: {{value: $sid, modifier: INCLUDES}}}},
                    filter: {{per_page: -1, sort: "date", direction: ASC}}
                ) {{ scenes {{ {scene_fields} }} }}
            }}"""
            res = await stash_query(q, {"sid": [studio_id]})
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            total_count = len(scenes)
            for scene in scenes[start_index:start_index + limit]:
                items.append(format_jellyfin_item(scene, parent_id=parent_id))
        else:
            q = """query FindSeriesScenes($sid: [ID!], $one: ID!) {
                findStudio(id: $one) { id name image_path }
                findScenes(
                    scene_filter: {studios: {value: $sid, modifier: INCLUDES}},
                    filter: {per_page: -1, sort: "date", direction: ASC}
                ) { scenes { id title } }
            }"""
            res = await stash_query(q, {"sid": [studio_id], "one": studio_id})
            studio = res.get("data", {}).get("findStudio") or {}
            series_name = studio.get("name") or f"Series {studio_id}"
            series_image = studio.get("image_path")
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])

            from stash_jellyfin_proxy.util.series import parse_episode
            seasons_seen = {}
            for scene in scenes:
                parsed = parse_episode(scene.get("title") or "")
                season_num = parsed[0] if parsed else 0
                seasons_seen.setdefault(season_num, 0)
                seasons_seen[season_num] += 1

            for season_num in sorted(seasons_seen.keys()):
                season_id = f"season-{studio_id}-{season_num}"
                season_label = f"Season {season_num}" if season_num else "Specials"
                items.append({
                    "Name": season_label,
                    "SortName": f"{season_num:04d}",
                    "Id": season_id,
                    "ServerId": runtime.SERVER_ID,
                    "Type": "Season",
                    "IsFolder": True,
                    "ParentId": parent_id,
                    "SeriesId": parent_id,
                    "SeriesName": series_name,
                    "IndexNumber": season_num,
                    "ChildCount": seasons_seen[season_num],
                    "RecursiveItemCount": seasons_seen[season_num],
                    "ImageTags": {"Primary": "img"},
                    "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
                    "BackdropImageTags": ["img"],
                    "UserData": {
                        "PlaybackPositionTicks": 0, "PlayCount": 0,
                        "IsFavorite": False, "Played": False,
                        "Key": season_id,
                    },
                })
            total_count = len(items)

    elif parent_id and parent_id.startswith("season-"):
        # season-<studio_id>-<season_num> → all scenes from that studio whose
        # parsed title season matches. Studio scenes that don't parse end up
        # in Season 0 (Specials).
        rest = parent_id.replace("season-", "", 1)
        try:
            studio_id, season_str = rest.rsplit("-", 1)
            want_season = int(season_str)
        except (ValueError, IndexError):
            logger.warning(f"Bad season id: {parent_id}")
            studio_id, want_season = "", -1

        if studio_id:
            q = f"""query FindSeasonScenes($sid: [ID!]) {{
                findScenes(
                    scene_filter: {{studios: {{value: $sid, modifier: INCLUDES}}}},
                    filter: {{per_page: -1, sort: "date", direction: ASC}}
                ) {{
                    scenes {{ {scene_fields} }}
                }}
            }}"""
            res = await stash_query(q, {"sid": [studio_id]})
            all_scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])

            from stash_jellyfin_proxy.util.series import parse_episode
            matched = []
            for scene in all_scenes:
                parsed = parse_episode(scene.get("title") or "")
                s_num = parsed[0] if parsed else 0
                if s_num == want_season:
                    matched.append(scene)

            total_count = len(matched)
            # Apply pagination on the in-memory list.
            for scene in matched[start_index:start_index + limit]:
                items.append(format_jellyfin_item(scene, parent_id=parent_id))
        else:
            total_count = 0

    elif parent_id == "root-scenes":
        # Filter-panel params: Genres, Tags, Years → scene_filter clause.
        filter_parts, filter_vars = await _filter_clause(request, filter_favorites)
        filter_body = ""
        filter_var_defs = ""
        filter_var_args = ""
        if filter_parts:
            filter_body = ", ".join(filter_parts)
            if "_filter_tag_ids" in filter_vars:
                filter_var_defs = ", $_filter_tag_ids: [ID!]"
                filter_var_args = ", _filter_tag_ids"

        # Count (filtered or total)
        if filter_body:
            count_q = f"""query CountFilteredScenes($_filter_tag_ids: [ID!]) {{
                findScenes(scene_filter: {{{filter_body}}}) {{ count }}
            }}"""
            cvars = {k: v for k, v in filter_vars.items()}
            count_res = await stash_query(count_q, cvars)
        else:
            count_q = """query { findScenes { count } }"""
            count_res = await stash_query(count_q)
        scene_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Check if there are saved filters for scenes (only if runtime.ENABLE_FILTERS is on)
        has_filters = False
        if runtime.ENABLE_FILTERS:
            saved_filters = await stash_get_saved_filters("SCENES")
            has_filters = len(saved_filters) > 0

        # On first page, add FILTERS folder at the top if there are saved filters
        # Detect client: Infuse supports folder browsing, others may not
        client_info = parse_emby_auth_header(request)
        client_name = client_info.get("Client", "").lower()
        client_supports_folders = "infuse" in client_name or "senplayer" in client_name
        show_filters = has_filters and (client_supports_folders or not restrict_to_movies)

        filters_added = False
        if start_index == 0 and show_filters:
            items.append(await format_filters_folder("root-scenes"))
            filters_added = True

        # Total count includes Filters folder if present
        show_filters_in_count = show_filters
        total_count = scene_count + 1 if show_filters_in_count else scene_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1

        # Reduce per_page when filters folder takes a slot, so total stays within limit
        fetch_limit = limit - 1 if filters_added else limit

        # Then get paginated scenes with sort + filter-panel params from request
        if filter_body:
            q = f"""query FindScenes($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!{filter_var_defs}) {{
                findScenes(
                    scene_filter: {{{filter_body}}},
                    filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                ) {{ scenes {{ {scene_fields} }} }}
            }}"""
            q_vars = {"page": page, "per_page": fetch_limit, "sort": sort_field, "direction": sort_direction}
            q_vars.update(filter_vars)
            res = await stash_query(q, q_vars)
        else:
            q = f"""query FindScenes($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                findScenes(filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                    scenes {{ {scene_fields} }}
                }}
            }}"""
            res = await stash_query(q, {"page": page, "per_page": fetch_limit, "sort": sort_field, "direction": sort_direction})
        for s in res.get("data", {}).get("findScenes", {}).get("scenes", []):
            items.append(format_jellyfin_item(s, parent_id="root-scenes"))

    elif parent_id == "root-studios":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

        # Filter parent-only studios (scene_count == 0). Network/holder
        # studios with no direct scenes make for confusing empty tiles.
        count_q = """query { findStudios(studio_filter: {scene_count: {value: 0, modifier: GREATER_THAN}}) { count } }"""
        count_res = await stash_query(count_q)
        studio_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

        has_filters = False
        if runtime.ENABLE_FILTERS:
            saved_filters = await stash_get_saved_filters("STUDIOS")
            has_filters = len(saved_filters) > 0

        filters_added = False
        if start_index == 0 and has_filters:
            items.append(await format_filters_folder("root-studios"))
            filters_added = True

        total_count = studio_count + 1 if has_filters else studio_count

        page = (start_index // limit) + 1
        fetch_limit = limit - 1 if filters_added else limit

        q = """query FindStudios($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findStudios(
                studio_filter: {scene_count: {value: 0, modifier: GREATER_THAN}},
                filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
            ) {
                studios { id name image_path scene_count }
            }
        }"""
        res = await stash_query(q, {"page": page, "per_page": fetch_limit, "sort": folder_sort, "direction": folder_dir})
        for s in res.get("data", {}).get("findStudios", {}).get("studios", []):
            studio_item = {
                "Name": s["name"],
                "SortName": sort_name_for(s["name"]),
                "Id": f"studio-{s['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": s.get("scene_count", 0),
                "RecursiveItemCount": s.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"studio-{s['id']}"}
            }
            if s.get("image_path"):
                studio_item["ImageTags"] = {"Primary": "img"}
                studio_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            else:
                studio_item["ImageTags"] = {}
            items.append(studio_item)

    elif parent_id and parent_id.startswith("studio-"):
        studio_id = parent_id.replace("studio-", "")

        # Get count for this studio
        count_q = """query CountScenes($sid: [ID!]) {
            findScenes(scene_filter: {studios: {value: $sid, modifier: INCLUDES}}) { count }
        }"""
        count_res = await stash_query(count_q, {"sid": [studio_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($sid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{studios: {{value: $sid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = await stash_query(q, {"sid": [studio_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Studio {studio_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-performers":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

        # Get total count
        count_q = """query { findPerformers { count } }"""
        count_res = await stash_query(count_q)
        performer_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

        # Check if there are saved filters for performers (only if runtime.ENABLE_FILTERS is on)
        has_filters = False
        if runtime.ENABLE_FILTERS:
            saved_filters = await stash_get_saved_filters("PERFORMERS")
            has_filters = len(saved_filters) > 0

        # On first page, add FILTERS folder at the top if there are saved filters
        filters_added = False
        if start_index == 0 and has_filters:
            items.append(await format_filters_folder("root-performers"))
            filters_added = True

        # Total count includes Filters folder if present
        total_count = performer_count + 1 if has_filters else performer_count

        # Calculate page - Stash uses 1-indexed pages
        page = (start_index // limit) + 1
        fetch_limit = limit - 1 if filters_added else limit

        q = """query FindPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findPerformers(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                performers { id name image_path scene_count favorite }
            }
        }"""
        res = await stash_query(q, {"page": page, "per_page": fetch_limit, "sort": folder_sort, "direction": folder_dir})
        for p in res.get("data", {}).get("findPerformers", {}).get("performers", []):
            performer_item = {
                "Name": p["name"],
                "SortName": sort_name_for(p["name"]),
                "Id": f"performer-{p['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": p.get("scene_count", 0),
                "RecursiveItemCount": p.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": bool(p.get("favorite")), "Played": False, "Key": f"performer-{p['id']}"}
            }
            if p.get("image_path"):
                performer_item["ImageTags"] = {"Primary": "img"}
                performer_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            else:
                performer_item["ImageTags"] = {}
            items.append(performer_item)

    elif parent_id and (parent_id.startswith("performer-") or parent_id.startswith("person-")):
        # Handle both performer- (from Performers list) and person- (from People in scene details)
        if parent_id.startswith("performer-"):
            performer_id = parent_id.replace("performer-", "")
        else:
            performer_id = parent_id.replace("person-", "")

        # Swiftfin's performer page fires parallel requests for Person,
        # BoxSet+UserView, Movie, Video, MusicVideo, Series, Episode —
        # one rail per match. Only emit scenes when the request asked for
        # Movie/Episode or no type filter. Video-only is suppressed so
        # Swiftfin doesn't render both "Movies" and "Videos" rails with
        # the same content (our scenes have MediaType=Video).
        scene_yielding_types = {"movie", "episode"}
        video_only = (
            "video" in include_types_lower
            and not any(t in scene_yielding_types for t in include_types_lower)
        )
        wants_scenes = (
            not include_type_list
            or any(t in scene_yielding_types for t in include_types_lower)
        )
        if video_only or not wants_scenes:
            logger.debug(
                f"Performer {performer_id}: skipped type filter "
                f"{include_type_list} (video_only={video_only})"
            )
            total_count = 0
        else:
            count_q = """query CountScenes($pid: [ID!]) {
                findScenes(scene_filter: {performers: {value: $pid, modifier: INCLUDES}}) { count }
            }"""
            count_res = await stash_query(count_q, {"pid": [performer_id]})
            total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

            page = (start_index // limit) + 1

            q = f"""query FindScenes($pid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                findScenes(
                    scene_filter: {{performers: {{value: $pid, modifier: INCLUDES}}}},
                    filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                ) {{
                    scenes {{ {scene_fields} }}
                }}
            }}"""
            res = await stash_query(q, {"pid": [performer_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            logger.debug(f"Performer {performer_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
            # If the request specifically asked for Episode only, keep only
            # SERIES-studio scenes; conversely Movie-only filters drop them.
            wants_episode_only = (
                "episode" in include_types_lower
                and "movie" not in include_types_lower
                and "video" not in include_types_lower
            )
            wants_movie_only = (
                ("movie" in include_types_lower or "video" in include_types_lower)
                and "episode" not in include_types_lower
            )
            for s in scenes:
                from stash_jellyfin_proxy.mapping.scene import is_series_scene
                is_ep = is_series_scene(s)
                if wants_episode_only and not is_ep:
                    continue
                if wants_movie_only and is_ep:
                    continue
                items.append(format_jellyfin_item(s, parent_id=parent_id))
            total_count = len(items) if (wants_episode_only or wants_movie_only) else total_count

    elif parent_id == "root-groups":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

        count_q = """query { findMovies { count } }"""
        count_res = await stash_query(count_q)
        group_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)

        has_filters = False
        if runtime.ENABLE_FILTERS:
            saved_filters = await stash_get_saved_filters("GROUPS")
            has_filters = len(saved_filters) > 0

        filters_added = False
        if start_index == 0 and has_filters:
            items.append(await format_filters_folder("root-groups"))
            filters_added = True

        total_count = group_count + 1 if has_filters else group_count

        FIXED_PAGE_SIZE = 50

        stash_page = (start_index // FIXED_PAGE_SIZE) + 1
        offset_within_page = start_index % FIXED_PAGE_SIZE
        items_needed = limit - 1 if filters_added else limit

        logger.debug(f"Groups pagination: startIndex={start_index}, limit={limit}, stash_page={stash_page}, offset_within_page={offset_within_page}")

        q = """query FindMovies($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findMovies(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                movies { id name scene_count tags { name } }
            }
        }"""

        fetched_movies = []
        current_page = stash_page
        while len(fetched_movies) < offset_within_page + items_needed:
            res = await stash_query(q, {"page": current_page, "per_page": FIXED_PAGE_SIZE, "sort": folder_sort, "direction": folder_dir})
            page_movies = res.get("data", {}).get("findMovies", {}).get("movies", [])
            if not page_movies:
                break
            fetched_movies.extend(page_movies)
            current_page += 1
            if current_page > stash_page + 1:
                break

        # Slice to get the items we need
        movies_to_return = fetched_movies[offset_within_page:offset_within_page + items_needed]

        # Log Y-groups for debugging
        y_groups = [m["name"] for m in movies_to_return if m.get("name", "").upper().startswith("Y")]
        if y_groups:
            logger.debug(f"Groups starting with Y in this batch: {y_groups}")

        logger.debug(f"Groups: fetched {len(fetched_movies)} total, returning {len(movies_to_return)} (offset {offset_within_page})")

        for m in movies_to_return:
            group_item = {
                "Name": m["name"],
                "SortName": sort_name_for(m["name"]),
                "Id": f"group-{m['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": m.get("scene_count", 0),
                "RecursiveItemCount": m.get("scene_count", 0),
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": is_group_favorite(m), "Played": False, "Key": f"group-{m['id']}"},
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
            }
            items.append(group_item)

    elif parent_id and parent_id.startswith("group-"):
        group_id = parent_id.replace("group-", "")

        # Get count for this group/movie
        count_q = """query CountScenes($mid: [ID!]) {
            findScenes(scene_filter: {movies: {value: $mid, modifier: INCLUDES}}) { count }
        }"""
        count_res = await stash_query(count_q, {"mid": [group_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($mid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{movies: {{value: $mid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = await stash_query(q, {"mid": [group_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Group {group_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id == "root-tags":
        # Tags folder: show Favorites, All Tags (if enabled), and saved tag filters
        items_count = 0

        # If the user has a saved tag filter named "Favorites" it takes
        # precedence over our synthetic tags-favorites folder (otherwise
        # the tab shows "Favorites" twice).
        saved_filters = await stash_get_saved_filters("TAGS")
        saved_filter_names_lc = {(sf.get("name") or "").strip().lower() for sf in saved_filters}
        show_synthetic_favorites = "favorites" not in saved_filter_names_lc
        show_synthetic_all = runtime.ENABLE_ALL_TAGS and "all tags" not in saved_filter_names_lc

        if show_synthetic_favorites:
            items.append({
                "Name": "Favorites",
                "SortName": "!1-Favorites",  # Sort to top
                "Id": "tags-favorites",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-favorites"}
            })
            items_count += 1

        if show_synthetic_all:
            items.append({
                "Name": "All Tags",
                "SortName": "!2-All Tags",  # Sort after Favorites
                "Id": "tags-all",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-all"}
            })
            items_count += 1

        # Show saved tag filters
        for sf in saved_filters:
            filter_id = sf.get("id")
            filter_name = sf.get("name", f"Filter {filter_id}")
            item_id = f"filter-tags-{filter_id}"
            items.append({
                "Name": filter_name,
                "SortName": filter_name,
                "Id": item_id,
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ParentId": parent_id,
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
            })
            items_count += 1

        total_count = items_count

    elif parent_id == "tags-favorites":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
        q = """query FindTags($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findTags(tag_filter: {favorite: true}, filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                count
                tags { id name scene_count image_path }
            }
        }"""
        page = (start_index // limit) + 1
        res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
        data = res.get("data", {}).get("findTags", {})
        total_count = data.get("count", 0)
        for t in data.get("tags", []):
            tag_item = {
                "Name": t["name"],
                "Id": f"tagitem-{t['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": t.get("scene_count", 0),
                "RecursiveItemCount": t.get("scene_count", 0),
                "ParentId": parent_id,
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": True, "Played": False, "Key": f"tagitem-{t['id']}"}
            }
            tag_item["ImageTags"] = {"Primary": "img"}
            tag_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            items.append(tag_item)

    elif parent_id == "tags-all":
        folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
        q = """query FindTags($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findTags(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                count
                tags { id name scene_count image_path favorite }
            }
        }"""
        page = (start_index // limit) + 1
        res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
        data = res.get("data", {}).get("findTags", {})
        total_count = data.get("count", 0)
        for t in data.get("tags", []):
            tag_item = {
                "Name": t["name"],
                "Id": f"tagitem-{t['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": "BoxSet",
                "IsFolder": True,
                "CollectionType": "movies",
                "ChildCount": t.get("scene_count", 0),
                "RecursiveItemCount": t.get("scene_count", 0),
                "ParentId": parent_id,
                "PrimaryImageAspectRatio": 0.6667,
                "BackdropImageTags": [],
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": t.get("favorite", False), "Played": False, "Key": f"tagitem-{t['id']}"}
            }
            tag_item["ImageTags"] = {"Primary": "img"}
            tag_item["ImageBlurHashes"] = {"Primary": {"img": "000000"}}
            items.append(tag_item)

    elif parent_id and parent_id.startswith("tagitem-"):
        # Browsing a specific tag - show scenes with this tag
        tag_id = parent_id.replace("tagitem-", "")

        # Get count for scenes with this tag
        count_q = """query CountScenes($tid: [ID!]) {
            findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
        }"""
        count_res = await stash_query(count_q, {"tid": [tag_id]})
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        # Calculate page
        page = (start_index // limit) + 1

        q = f"""query FindScenes($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
            findScenes(
                scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
            ) {{
                scenes {{ {scene_fields} }}
            }}
        }}"""
        res = await stash_query(q, {"tid": [tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
        scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
        logger.debug(f"Tag {tag_id} returned {len(scenes)} scenes (page {page}, total {total_count})")
        for s in scenes:
            items.append(format_jellyfin_item(s, parent_id=parent_id))

    elif parent_id and parent_id.startswith("tag-"):
        # Tag-based folder: find scenes with this tag (from runtime.TAG_GROUPS config)
        # Extract tag name from parent_id (reverse the slugification)
        tag_slug = parent_id[4:]  # Remove "tag-" prefix

        # Find the matching tag name from runtime.TAG_GROUPS config
        tag_name = None
        for t in runtime.TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break

        if tag_name:
            # First we need to find the tag ID by name
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) {
                    tags { id name }
                }
            }"""
            tag_res = await stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])

            # Find exact match (case-insensitive)
            tag_id = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_id = t["id"]
                    break

            if tag_id:
                # Get count for scenes with this tag
                count_q = """query CountScenes($tid: [ID!]) {
                    findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
                }"""
                count_res = await stash_query(count_q, {"tid": [tag_id]})
                total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

                # Calculate page
                page = (start_index // limit) + 1

                q = f"""query FindScenes($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                    findScenes(
                        scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                        filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                    ) {{
                        scenes {{ {scene_fields} }}
                    }}
                }}"""
                res = await stash_query(q, {"tid": [tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                logger.debug(f"Tag '{tag_name}' (id={tag_id}) returned {len(scenes)} scenes (page {page}, total {total_count})")
                for s in scenes:
                    items.append(format_jellyfin_item(s, parent_id=parent_id))
            else:
                logger.warning(f"Tag '{tag_name}' not found in Stash")
        else:
            logger.warning(f"Tag slug '{tag_slug}' not found in runtime.TAG_GROUPS config")

    elif not parent_id and not ids and not person_ids and not search_term:
        # Global query with no parent - used by clients for home screen, search filters, etc.
        # Distinguish Movie (→ Groups/BoxSets) from Video (→ Scenes) to avoid duplicates.
        # Do NOT return anything for Series/Episode-only requests (Stash only has movie-type content).
        movie_only = bool(include_type_list) and "movie" in include_types_lower and "video" not in include_types_lower
        video_requested = "video" in include_types_lower or not include_type_list  # no type filter = return scenes

        if not has_movie_type:
            logger.debug(f"Global query skipped - requested types {include_type_list} don't include Movie/Video")
        elif movie_only:
            # Banner detection: some clients (SenPlayer) request the home-screen
            # rotating banner with Movie-only + SortBy containing "Random". Return
            # randomized Scenes (with screenshots) instead of Groups for better visuals.
            sort_by_raw = request.query_params.get("SortBy") or request.query_params.get("sortBy") or ""
            is_banner_request = "random" in sort_by_raw.lower() and not filter_favorites
            if is_banner_request:
                banner_scenes = []
                tag_ids = []
                if runtime.BANNER_MODE == "tag" and runtime.BANNER_TAGS:
                    # Stash's name filter doesn't accept a list; look up each tag individually.
                    for tname in runtime.BANNER_TAGS:
                        try:
                            res = await stash_query(
                                """query FindTag($n: String!) { findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}) { tags { id name } } }""",
                                {"n": tname},
                            )
                            for t in res.get("data", {}).get("findTags", {}).get("tags", []):
                                if t["name"].lower() == tname.lower():
                                    tag_ids.append(t["id"])
                                    break
                        except Exception as e:
                            logger.warning(f"Banner tag lookup failed for '{tname}': {e}")
                    if not tag_ids:
                        logger.debug(f"Banner mode=tag but no runtime.BANNER_TAGS resolved ({runtime.BANNER_TAGS}); falling back to recent")

                pool = await _hero_pool(scene_fields, tag_ids)

                if pool:
                    banner_scenes = random.sample(pool, min(limit, len(pool)))
                for s in banner_scenes:
                    items.append(format_jellyfin_item(s))
                total_count = len(items)
                # Skip the Groups branch entirely for banner requests.
                return JSONResponse({
                    "Items": items,
                    "TotalRecordCount": total_count,
                    "StartIndex": start_index,
                })

            # Movie type only → return Groups (BoxSets), not scenes
            folder_sort, folder_dir = get_stash_sort_params(request, context="folders")
            if filter_favorites and runtime.FAVORITE_TAG:
                # User-favorites (toggled via /Users/.../FavoriteItems) tag SCENES
                # with FAVORITE_TAG — not Groups. Return scenes as Type: Movie so
                # SenPlayer's Movie+IsFavorite query populates correctly. The
                # empty `movies = []` below is because scenes are emitted via
                # format_jellyfin_item and appended directly; the Groups loop
                # below it gets no input and is skipped.
                fav_tag_id = await get_or_create_tag(runtime.FAVORITE_TAG)
                if fav_tag_id:
                    count_q = """query CountFavScenes($tid: [ID!]) {
                        findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
                    }"""
                    count_res = await stash_query(count_q, {"tid": [fav_tag_id]})
                    total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
                    page = (start_index // limit) + 1
                    q = f"""query FindFavScenesMovie($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                        findScenes(
                            scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                            filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                        ) {{
                            scenes {{ {scene_fields} }}
                        }}
                    }}"""
                    res = await stash_query(q, {"tid": [fav_tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Movie+IsFavorite returned {len(scenes)} favorited scenes (page {page}, total {total_count})")
                    for s in scenes:
                        items.append(format_jellyfin_item(s))
                else:
                    logger.warning(f"IsFavorite filter requested but could not resolve runtime.FAVORITE_TAG '{runtime.FAVORITE_TAG}'")
                movies = []
            elif filter_favorites and not runtime.FAVORITE_TAG:
                logger.debug("Movie+IsFavorite: runtime.FAVORITE_TAG not configured - returning empty")
                movies = []
                total_count = 0
            else:
                count_q = "query { findMovies { count } }"
                count_res = await stash_query(count_q)
                total_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)
                page = (start_index // limit) + 1
                q = """query FindMovies($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                    findMovies(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                        movies { id name scene_count tags { name } }
                    }
                }"""
                res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
                movies = res.get("data", {}).get("findMovies", {}).get("movies", [])
                logger.debug(f"Global Movie query returned {len(movies)} groups (page {page}, total {total_count})")
            for m in movies:
                items.append({
                    "Name": m["name"],
                    "Id": f"group-{m['id']}",
                    "ServerId": runtime.SERVER_ID,
                    "Type": "BoxSet",
                    "IsFolder": True,
                    "CollectionType": "movies",
                    "ChildCount": m.get("scene_count", 0),
                    "PrimaryImageAspectRatio": 0.6667,
                    "BackdropImageTags": [],
                    "ImageTags": {"Primary": "img"},
                    "ImageBlurHashes": {"Primary": {"img": "000000"}},
                    "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": is_group_favorite(m), "Played": False, "Key": f"group-{m['id']}"}
                })
        elif video_requested:
            # Jellyfin Web's Favorites tab queries separately for each type
            # (Movie, Video, Episode, …) and renders a rail per hit. Our scenes
            # are typed Movie, so a bare Video-only favorites query would
            # duplicate them into a "Videos" rail next to "Movies". Skip.
            if filter_favorites and "video" in include_types_lower and "movie" not in include_types_lower:
                logger.debug("Video-only favorites query suppressed to avoid Movies/Videos duplication")
                total_count = 0
            # Video type (or no type filter) → return Scenes
            elif filter_favorites and runtime.FAVORITE_TAG:
                fav_tag_id = await get_or_create_tag(runtime.FAVORITE_TAG)
                if fav_tag_id:
                    count_q = """query CountFavScenes($tid: [ID!]) {
                        findScenes(scene_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count }
                    }"""
                    count_res = await stash_query(count_q, {"tid": [fav_tag_id]})
                    total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
                    page = (start_index // limit) + 1
                    q = f"""query FindFavScenes($tid: [ID!], $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                        findScenes(
                            scene_filter: {{tags: {{value: $tid, modifier: INCLUDES}}}},
                            filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}
                        ) {{
                            scenes {{ {scene_fields} }}
                        }}
                    }}"""
                    res = await stash_query(q, {"tid": [fav_tag_id], "page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                    scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                    logger.debug(f"Favorites query returned {len(scenes)} scenes (page {page}, total {total_count})")
                    for s in scenes:
                        items.append(format_jellyfin_item(s))
                else:
                    logger.warning(f"IsFavorite filter requested but could not resolve runtime.FAVORITE_TAG '{runtime.FAVORITE_TAG}'")
            elif filter_favorites and not runtime.FAVORITE_TAG:
                logger.debug("IsFavorite filter requested but runtime.FAVORITE_TAG not configured - returning empty")
                total_count = 0
            else:
                count_q = "query { findScenes { count } }"
                count_res = await stash_query(count_q)
                total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)
                page = (start_index // limit) + 1
                q = f"""query FindScenes($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {{
                    findScenes(filter: {{page: $page, per_page: $per_page, sort: $sort, direction: $direction}}) {{
                        scenes {{ {scene_fields} }}
                    }}
                }}"""
                res = await stash_query(q, {"page": page, "per_page": limit, "sort": sort_field, "direction": sort_direction})
                scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
                logger.debug(f"Global query returned {len(scenes)} scenes (page {page}, total {total_count})")
                for s in scenes:
                    items.append(format_jellyfin_item(s))

    # Log pagination info for debugging
    logger.debug(f"Items response: returning {len(items)} items, TotalRecordCount={total_count}, StartIndex={start_index}")
    if len(items) > 0 and total_count > start_index + len(items):
        logger.debug(f"More items available: next page would start at {start_index + len(items)}")

    response_data = {"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index}
    return JSONResponse(response_data)


async def _fetch_performer_packet(performer_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the rich performer packet and shape it for Jellyfin's About
    panel. Stash has no free-form Overview text most of the time, so we
    synthesize a readable description from the structured attributes
    (gender, age, country, measurements, career span, etc.) so Swiftfin's
    performer page isn't blank. Returns None if the performer doesn't
    exist."""
    q = """query PerformerPacket($id: ID!) {
        findPerformer(id: $id) {
            id name disambiguation gender birthdate death_date
            ethnicity country hair_color eye_color
            height_cm weight measurements fake_tits
            career_start career_end tattoos piercings
            alias_list details rating100 favorite scene_count image_path
            tags { id name }
            stash_ids { endpoint stash_id }
        }
    }"""
    res = await stash_query(q, {"id": performer_id})
    performer = ((res or {}).get("data") or {}).get("findPerformer")
    if not performer:
        return None

    out: Dict[str, Any] = {}

    # Structured Overview — one short sentence summarising the performer,
    # then paragraphs for the free-form details / aliases. Keeps Swiftfin's
    # About panel populated even when Stash has no hand-written bio.
    summary_bits = []
    gender = (performer.get("gender") or "").lower()
    if gender == "female":
        summary_bits.append("Female performer")
    elif gender == "male":
        summary_bits.append("Male performer")
    elif gender:
        summary_bits.append(gender.replace("_", " ").capitalize() + " performer")
    else:
        summary_bits.append("Performer")

    bd = performer.get("birthdate")
    if bd:
        try:
            import datetime as _dt
            today = _dt.date.today()
            birth = _dt.date.fromisoformat(bd)
            age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            summary_bits[-1] += f", born {bd} ({age})" if not performer.get("death_date") else f", born {bd}"
        except Exception:
            pass

    country = performer.get("country")
    if country:
        summary_bits.append(f"from {country}")

    c_start = performer.get("career_start")
    c_end = performer.get("career_end")
    if c_start and c_end and c_start != c_end:
        summary_bits.append(f"active {c_start}–{c_end}")
    elif c_start:
        summary_bits.append(f"active since {c_start}")

    scene_count = int(performer.get("scene_count") or 0)
    if scene_count:
        summary_bits.append(f"{scene_count} scene{'s' if scene_count != 1 else ''} in library")

    parts: list[str] = []
    parts.append(", ".join(summary_bits) + ".")

    # Physical attributes — second paragraph, only emit keys with values.
    phys: list[str] = []
    if performer.get("height_cm"):
        cm = int(performer["height_cm"])
        inches = round(cm / 2.54)
        phys.append(f"Height: {cm} cm ({inches // 12}'{inches % 12}\")")
    if performer.get("weight"):
        phys.append(f"Weight: {performer['weight']} kg")
    if performer.get("measurements"):
        phys.append(f"Measurements: {performer['measurements']}")
    if performer.get("fake_tits"):
        phys.append(f"Breasts: {performer['fake_tits']}")
    if performer.get("ethnicity"):
        phys.append(f"Ethnicity: {performer['ethnicity']}")
    if performer.get("hair_color"):
        phys.append(f"Hair: {performer['hair_color']}")
    if performer.get("eye_color"):
        phys.append(f"Eyes: {performer['eye_color']}")
    if phys:
        parts.append("\n".join(phys))

    # Body-mod notes.
    mods: list[str] = []
    if performer.get("tattoos"):
        mods.append(f"Tattoos: {performer['tattoos']}")
    if performer.get("piercings"):
        mods.append(f"Piercings: {performer['piercings']}")
    if mods:
        parts.append("\n".join(mods))

    aliases = [a for a in (performer.get("alias_list") or []) if a]
    if aliases:
        parts.append(f"Also known as: {', '.join(aliases)}")

    if performer.get("details"):
        # Prepend hand-written bio if present.
        parts.insert(0, performer["details"].strip())

    out["Overview"] = "\n\n".join(parts)

    if performer.get("rating100") is not None:
        try:
            out["CommunityRating"] = round(float(performer["rating100"]) / 10.0, 1)
        except (TypeError, ValueError):
            pass

    # Birthday as PremiereDate / ProductionYear for Swiftfin's "born" field.
    if performer.get("birthdate"):
        try:
            out["PremiereDate"] = f"{performer['birthdate']}T00:00:00.0000000Z"
            out["ProductionYear"] = int(performer["birthdate"][:4])
        except (ValueError, TypeError):
            pass
    if performer.get("death_date"):
        try:
            out["EndDate"] = f"{performer['death_date']}T00:00:00.0000000Z"
        except (ValueError, TypeError):
            pass

    tag_names = [
        (t.get("name") or "").strip()
        for t in (performer.get("tags") or [])
        if t.get("name")
    ]
    if tag_names:
        out["Genres"] = tag_names
        out["Tags"] = tag_names

    stash_ids = performer.get("stash_ids") or []
    if stash_ids and stash_ids[0].get("stash_id"):
        out["ProviderIds"] = {"StashDb": stash_ids[0]["stash_id"]}

    out["_favorite"] = bool(performer.get("favorite"))
    out["_scene_count"] = scene_count
    out["_name"] = performer.get("name") or f"Performer {performer_id}"
    out["_has_image"] = bool(performer.get("image_path"))
    return out


async def _fetch_studio_packet(studio_id: str) -> Optional[Dict[str, Any]]:
    """Fetch the rich studio packet from Stash and shape it for Jellyfin's
    About section. Returns None if the studio doesn't exist.

    Covers: Overview (details + aliases + homepage), CommunityRating,
    ProductionYear/EndDate (from earliest/latest scene dates), parent studio
    (as Studios[]), tags mapped to Genres/Tags, favorite flag, StashDb
    provider id."""
    q = """query StudioPacket($id: ID!, $sid: [ID!]) {
        findStudio(id: $id) {
            id name url details aliases rating100 favorite scene_count
            parent_studio { id name }
            tags { id name }
            stash_ids { endpoint stash_id }
        }
        earliest: findScenes(
            scene_filter: {studios: {value: $sid, modifier: INCLUDES}},
            filter: {page: 1, per_page: 1, sort: "date", direction: ASC}
        ) { scenes { date } }
        latest: findScenes(
            scene_filter: {studios: {value: $sid, modifier: INCLUDES}},
            filter: {page: 1, per_page: 1, sort: "date", direction: DESC}
        ) { scenes { date } }
    }"""
    res = await stash_query(q, {"id": studio_id, "sid": [studio_id]})
    data = (res or {}).get("data") or {}
    studio = data.get("findStudio")
    if not studio:
        return None

    out: Dict[str, Any] = {}

    # Overview — details + aliases. Homepage goes to ExternalUrls only so
    # it doesn't pollute the description text. When Stash has no details
    # and no aliases, fall back to a synthesized one-liner from the counts
    # + year range so Swiftfin's About panel isn't empty.
    overview_parts = []
    if studio.get("details"):
        overview_parts.append(studio["details"].strip())
    aliases = [a for a in (studio.get("aliases") or []) if a]
    if aliases:
        overview_parts.append(f"Also known as: {', '.join(aliases)}")
    if overview_parts:
        out["Overview"] = "\n\n".join(overview_parts)

    # Rating — Stash rating100 (0–100) → Jellyfin CommunityRating (0–10).
    if studio.get("rating100") is not None:
        try:
            out["CommunityRating"] = round(float(studio["rating100"]) / 10.0, 1)
        except (TypeError, ValueError):
            pass

    # Parent studio — rendered as Studios[] so Swiftfin's About shows it
    # under the Network/Studio field.
    parent = studio.get("parent_studio") or {}
    if parent.get("id") and parent.get("name"):
        out["Studios"] = [{"Name": parent["name"], "Id": f"studio-{parent['id']}"}]

    # Tags — strip the SERIES marker (it's plumbing, not user-facing), map
    # the rest to Jellyfin Genres + Tags.
    series_tag_lc = (runtime.SERIES_TAG or "").lower()
    tag_names = [
        (t.get("name") or "").strip()
        for t in (studio.get("tags") or [])
        if t.get("name") and (t.get("name") or "").lower() != series_tag_lc
    ]
    if tag_names:
        out["Genres"] = tag_names
        out["Tags"] = tag_names

    # Year range — earliest scene → ProductionYear, latest → EndDate.
    earliest_scenes = (data.get("earliest") or {}).get("scenes") or []
    latest_scenes = (data.get("latest") or {}).get("scenes") or []
    if earliest_scenes and earliest_scenes[0].get("date"):
        date_str = earliest_scenes[0]["date"]
        try:
            out["ProductionYear"] = int(date_str[:4])
            out["PremiereDate"] = f"{date_str}T00:00:00.0000000Z"
        except (ValueError, TypeError):
            pass
    if latest_scenes and latest_scenes[0].get("date"):
        date_str = latest_scenes[0]["date"]
        try:
            out["EndDate"] = f"{date_str}T00:00:00.0000000Z"
        except (ValueError, TypeError):
            pass

    # Homepage as an external link.
    if studio.get("url"):
        out["ExternalUrls"] = [{"Name": "Homepage", "Url": studio["url"]}]

    # StashDb provider id for client-side deep linking.
    stash_ids = studio.get("stash_ids") or []
    if stash_ids and stash_ids[0].get("stash_id"):
        out["ProviderIds"] = {"StashDb": stash_ids[0]["stash_id"]}

    # Synthesize a fallback Overview when Stash has none, so Swiftfin's
    # About panel has something to render. Uses whatever packet data we
    # already extracted (year range, scene count, parent).
    if "Overview" not in out:
        scene_count = int(studio.get("scene_count") or 0)
        start_year = out.get("ProductionYear")
        end_year = None
        if out.get("EndDate"):
            try:
                end_year = int(out["EndDate"][:4])
            except (ValueError, TypeError):
                pass
        bits = []
        if scene_count:
            bits.append(f"{scene_count} title{'s' if scene_count != 1 else ''}")
        if start_year and end_year and end_year != start_year:
            bits.append(f"{start_year}–{end_year}")
        elif start_year:
            bits.append(f"{start_year}")
        parent = studio.get("parent_studio") or {}
        if parent.get("name"):
            bits.append(f"part of the {parent['name']} network")
        if bits:
            out["Overview"] = f"{studio.get('name') or 'This collection'} — " + ", ".join(bits) + "."

    # Core counts + favorite state — callers merge these into the envelope.
    out["_favorite"] = bool(studio.get("favorite"))
    out["_scene_count"] = int(studio.get("scene_count") or 0)
    out["_name"] = studio.get("name") or f"Studio {studio_id}"
    return out


async def endpoint_item_details(request):
    from stash_jellyfin_proxy.mapping.genre import genre_allowed_names
    await genre_allowed_names()
    item_id = request.path_params.get("item_id")

    # Full scene fields for queries (include performer image_path for People images, captions for subtitles)
    scene_fields = "id title code date details play_count resume_time last_played_at files { path basename duration size video_codec audio_codec width height frame_rate bit_rate } studio { id name tags { name } parent_studio { id name tags { name } } } tags { name } performers { name id image_path } captions { language_code caption_type } stash_ids { stash_id }"

    # Handle special folder IDs - return the folder ITSELF (not children)

    # Handle FILTERS folder details
    if item_id.startswith("filters-"):
        filter_mode = item_id.replace("filters-", "").upper()
        saved_filters = await stash_get_saved_filters(filter_mode)
        filter_count = len(saved_filters)

        mode_names = {"SCENES": "Scenes", "PERFORMERS": "Performers", "STUDIOS": "Studios", "GROUPS": "Groups"}
        mode_name = mode_names.get(filter_mode, filter_mode.capitalize())

        return JSONResponse({
            "Name": "FILTERS",
            "SortName": "!!!FILTERS",
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": filter_count,
            "RecursiveItemCount": filter_count,
            "Overview": f"Saved filters for {mode_name}",
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
        })

    # Handle individual saved filter details
    if item_id.startswith("filter-"):
        parts = item_id.split("-", 2)
        if len(parts) == 3:
            filter_mode = parts[1].upper()
            filter_id = parts[2]

            # Get the saved filter details
            query = """query FindSavedFilter($id: ID!) {
                findSavedFilter(id: $id) { id name mode }
            }"""
            res = await stash_query(query, {"id": filter_id})
            saved_filter = res.get("data", {}).get("findSavedFilter")

            if saved_filter:
                filter_name = saved_filter.get("name", f"Filter {filter_id}")

                return JSONResponse({
                    "Name": filter_name,
                    "SortName": filter_name,
                    "Id": item_id,
                    "ServerId": runtime.SERVER_ID,
                    "Type": "BoxSet",
                    "CollectionType": "movies",
                    "IsFolder": True,
                    "ImageTags": {"Primary": "img"},
                    "ImageBlurHashes": {"Primary": {"img": "000000"}},
                    "PrimaryImageAspectRatio": 0.6667,
                    "BackdropImageTags": [],
                    "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
                })

    if item_id == "root-series":
        total_count = 0
        if runtime.SERIES_TAG:
            tag_id = await get_or_create_tag(runtime.SERIES_TAG)
            if tag_id:
                count_q = """query Cnt($tid: [ID!]) { findStudios(studio_filter: {tags: {value: $tid, modifier: INCLUDES}}) { count } }"""
                count_res = await stash_query(count_q, {"tid": [tag_id]})
                total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)
        # Per-client CollectionType: Swiftfin gets tvshows for native Series
        # nav; Infuse/SenPlayer render tvshows as unnamed folders so they get
        # movies (name shows, tapping in lists series as BoxSets).
        from stash_jellyfin_proxy.players.matcher import resolve_from_request
        series_ct = "tvshows" if resolve_from_request(request).name == "swiftfin" else "movies"
        return JSONResponse({
            "Name": "Series",
            "SortName": "Series",
            "Id": "root-series",
            "ServerId": runtime.SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": series_ct,
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-series"}
        })

    elif item_id.startswith("series-"):
        studio_id = item_id.replace("series-", "")
        packet = await _fetch_studio_packet(studio_id)
        if not packet:
            return JSONResponse({"error": "Series not found"}, status_code=404)
        name = packet.pop("_name")
        scene_count = packet.pop("_scene_count")
        is_favorite = packet.pop("_favorite")
        out = {
            "Name": name,
            "SortName": name,
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "Series",
            "IsFolder": True,
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "PrimaryImageAspectRatio": 0.6667,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
            "BackdropImageTags": ["img"],
            "UserData": {
                "PlaybackPositionTicks": 0, "PlayCount": 0,
                "IsFavorite": is_favorite, "Played": False, "Key": item_id,
            },
        }
        out.update(packet)
        # Status — if the most recent scene is within the last 18 months,
        # call it "Continuing"; older than that → "Ended". Swiftfin renders
        # this on the Series About screen.
        if packet.get("EndDate"):
            import datetime as _dt
            try:
                end_dt = _dt.datetime.fromisoformat(packet["EndDate"].replace("Z", "+00:00")[:19] + "+00:00")
                now = _dt.datetime.now(_dt.timezone.utc)
                out["Status"] = "Continuing" if (now - end_dt).days <= 540 else "Ended"
            except (ValueError, TypeError):
                pass
        return JSONResponse(out)

    elif item_id.startswith("season-"):
        rest = item_id.replace("season-", "", 1)
        try:
            studio_id, season_str = rest.rsplit("-", 1)
            season_num = int(season_str)
        except (ValueError, IndexError):
            return JSONResponse({"error": "Bad season id"}, status_code=404)
        q = """query FindStudio($id: ID!) { findStudio(id: $id) { id name image_path } }"""
        res = await stash_query(q, {"id": studio_id})
        studio = res.get("data", {}).get("findStudio")
        if not studio:
            return JSONResponse({"error": "Season not found"}, status_code=404)
        label = f"Season {season_num}" if season_num else "Specials"
        return JSONResponse({
            "Name": label,
            "SortName": f"{season_num:04d}",
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "Season",
            "IsFolder": True,
            "ParentId": f"series-{studio_id}",
            "SeriesId": f"series-{studio_id}",
            "SeriesName": studio["name"],
            "IndexNumber": season_num,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
            "BackdropImageTags": ["img"],
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id},
        })

    if item_id == "root-scenes":
        # Get actual count
        count_q = """query { findScenes { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findScenes", {}).get("count", 0)

        return JSONResponse({
            "Name": "All Scenes",
            "SortName": "All Scenes",
            "Id": "root-scenes",
            "ServerId": runtime.SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-scenes"}
        })

    elif item_id == "root-studios":
        # Get actual count
        count_q = """query { findStudios { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)

        return JSONResponse({
            "Name": "Studios",
            "SortName": "Studios",
            "Id": "root-studios",
            "ServerId": runtime.SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-studios"}
        })

    elif item_id.startswith("studio-"):
        # Fetch actual studio info from Stash
        studio_id = item_id.replace("studio-", "")
        packet = await _fetch_studio_packet(studio_id)
        if not packet:
            return JSONResponse({"error": "Studio not found"}, status_code=404)
        studio_name = packet.pop("_name")
        scene_count = packet.pop("_scene_count")
        is_favorite = packet.pop("_favorite")
        out = {
            "Name": studio_name,
            "SortName": sort_name_for(studio_name),
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": ["img"],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {
                "PlaybackPositionTicks": 0, "PlayCount": 0,
                "IsFavorite": is_favorite, "Played": False, "Key": item_id,
            },
        }
        out.update(packet)
        return JSONResponse(out)

    elif item_id == "root-performers":
        # Get actual count
        count_q = """query { findPerformers { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)

        return JSONResponse({
            "Name": "Performers",
            "SortName": "Performers",
            "Id": "root-performers",
            "ServerId": runtime.SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-performers"}
        })

    elif item_id.startswith("performer-") or item_id.startswith("person-"):
        # Handle id formats: performer-302, person-302, person-performer-302
        if item_id.startswith("person-performer-"):
            performer_id = item_id.replace("person-performer-", "")
        elif item_id.startswith("performer-"):
            performer_id = item_id.replace("performer-", "")
        else:
            performer_id = item_id.replace("person-", "")

        packet = await _fetch_performer_packet(performer_id)
        if not packet:
            logger.warning(f"Performer not found: {performer_id}")
            return JSONResponse({"Items": [], "TotalRecordCount": 0}, status_code=404)

        performer_name = packet.pop("_name")
        scene_count = packet.pop("_scene_count")
        is_favorite = packet.pop("_favorite")
        has_image = packet.pop("_has_image")

        from stash_jellyfin_proxy.mapping.image_policy import performer_item_type
        item_type = performer_item_type(request)  # "Person" for Swiftfin, else "BoxSet"

        out = {
            "Name": performer_name,
            "SortName": sort_name_for(performer_name),
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": item_type,
            "IsFolder": True,
            "ImageTags": {"Primary": "img"} if has_image else {},
            "ImageBlurHashes": ({"Primary": {"img": "000000"}, "Backdrop": {"img": "000000"}} if has_image else {}),
            "PrimaryImageAspectRatio": 0.6667,
            # BackdropImageTags populated so Swiftfin's performer-page hero
            # banner actually fires the backdrop request.
            "BackdropImageTags": ["img"] if has_image else [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {
                "PlaybackPositionTicks": 0, "PlayCount": 0,
                "IsFavorite": is_favorite, "Played": False, "Key": item_id,
            },
        }
        # BoxSet-typed performers (Infuse/SenPlayer) need CollectionType for
        # the grid renderer to work. Person-typed performers skip it — Swiftfin
        # renders its native Person screen which doesn't use CollectionType.
        if item_type == "BoxSet":
            out["CollectionType"] = "movies"
        out.update(packet)
        return JSONResponse(out)

    elif item_id == "root-groups":
        # Get actual count
        count_q = """query { findMovies { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findMovies", {}).get("count", 0)

        return JSONResponse({
            "Name": "Groups",
            "SortName": "Groups",
            "Id": "root-groups",
            "ServerId": runtime.SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {},
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-groups"}
        })

    elif item_id.startswith("group-"):
        # Fetch actual group/movie info from Stash
        group_id = item_id.replace("group-", "")
        q = """query FindMovie($id: ID!) { findMovie(id: $id) { id name front_image_path scene_count tags { name } } }"""
        res = await stash_query(q, {"id": group_id})
        group = res.get("data", {}).get("findMovie", {})

        group_name = group.get("name", f"Group {group_id}")
        scene_count = group.get("scene_count", 0)
        has_image = bool(group.get("front_image_path"))

        return JSONResponse({
            "Name": group_name,
            "SortName": sort_name_for(group_name),
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": is_group_favorite(group), "Played": False, "Key": item_id}
        })

    elif item_id == "root-tags":
        # Tags folder details
        # Count is Favorites + (All Tags if enabled) + saved filters count
        count = 1  # Favorites
        if runtime.ENABLE_ALL_TAGS:
            count += 1
        saved_filters = await stash_get_saved_filters("TAGS")
        count += len(saved_filters)

        return JSONResponse({
            "Name": "Tags",
            "SortName": "Tags",
            "Id": "root-tags",
            "ServerId": runtime.SERVER_ID,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "icon"},
            "BackdropImageTags": [],
            "ChildCount": count,
            "RecursiveItemCount": count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "root-tags"}
        })

    elif item_id == "tags-favorites":
        # Favorites subfolder details
        count_q = """query { findTags(tag_filter: {favorite: true}) { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findTags", {}).get("count", 0)

        return JSONResponse({
            "Name": "Favorites",
            "SortName": "!1-Favorites",
            "Id": "tags-favorites",
            "ServerId": runtime.SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-favorites"}
        })

    elif item_id == "tags-all":
        # All Tags subfolder details
        count_q = """query { findTags { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findTags", {}).get("count", 0)

        return JSONResponse({
            "Name": "All Tags",
            "SortName": "!2-All Tags",
            "Id": "tags-all",
            "ServerId": runtime.SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": total_count,
            "RecursiveItemCount": total_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": "tags-all"}
        })

    elif item_id.startswith("tagitem-"):
        # Individual tag details
        tag_id = item_id.replace("tagitem-", "")
        q = """query FindTag($id: ID!) { findTag(id: $id) { id name scene_count image_path favorite } }"""
        res = await stash_query(q, {"id": tag_id})
        tag = res.get("data", {}).get("findTag")

        if not tag:
            logger.warning(f"Tag not found: {tag_id}")
            return JSONResponse({"error": "Tag not found"}, status_code=404)

        tag_name = tag.get("name", f"Tag {tag_id}")
        scene_count = tag.get("scene_count", 0)
        has_image = bool(tag.get("image_path"))

        return JSONResponse({
            "Name": tag_name,
            "SortName": tag_name,
            "Id": item_id,
            "ServerId": runtime.SERVER_ID,
            "Type": "BoxSet",
            "CollectionType": "movies",
            "IsFolder": True,
            "ImageTags": {"Primary": "img"},
            "ImageBlurHashes": {"Primary": {"img": "000000"}},
            "PrimaryImageAspectRatio": 0.6667,
            "BackdropImageTags": [],
            "ChildCount": scene_count,
            "RecursiveItemCount": scene_count,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": tag.get("favorite", False), "Played": False, "Key": item_id}
        })

    elif item_id.startswith("tag-"):
        # Tag-based folder (from runtime.TAG_GROUPS config)
        tag_slug = item_id[4:]  # Remove "tag-" prefix

        # Find the matching tag name from runtime.TAG_GROUPS config
        tag_name = None
        for t in runtime.TAG_GROUPS:
            if t.lower().replace(' ', '-') == tag_slug:
                tag_name = t
                break

        if tag_name:
            # Find tag ID and get scene count
            tag_query = """query FindTags($filter: FindFilterType!) {
                findTags(filter: $filter) {
                    tags { id name scene_count }
                }
            }"""
            tag_res = await stash_query(tag_query, {"filter": {"q": tag_name}})
            tags = tag_res.get("data", {}).get("findTags", {}).get("tags", [])

            # Find exact match
            tag_data = None
            for t in tags:
                if t["name"].lower() == tag_name.lower():
                    tag_data = t
                    break

            scene_count = tag_data.get("scene_count", 0) if tag_data else 0

            return JSONResponse({
                "Name": tag_name,
                "SortName": tag_name,
                "Id": item_id,
                "ServerId": runtime.SERVER_ID,
                "Type": "CollectionFolder",
                "CollectionType": "movies",
                "IsFolder": True,
                "ImageTags": {"Primary": "icon"},
                "BackdropImageTags": [],
                "ChildCount": scene_count,
                "RecursiveItemCount": scene_count,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": item_id}
            })
        else:
            logger.warning(f"Tag slug '{tag_slug}' not found in runtime.TAG_GROUPS config")
            return JSONResponse({"error": "Tag not found"}, status_code=404)

    elif item_id in ("Resume", "Latest"):
        # Return empty for resume/latest
        return JSONResponse({"Items": [], "TotalRecordCount": 0})

    # Otherwise it's a scene ID (scene-123 format) - extract numeric for Stash query
    if item_id.startswith("scene-"):
        numeric_id = item_id.replace("scene-", "")
    else:
        numeric_id = extract_numeric_id(item_id)

    q = f"""query FindScene($id: ID!) {{ findScene(id: $id) {{ {scene_fields} }} }}"""
    res = await stash_query(q, {"id": numeric_id})
    scene = res.get("data", {}).get("findScene")
    if not scene:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(format_jellyfin_item(scene))


