"""Search + taxonomic-list endpoints backed by Stash GraphQL queries.

Includes the counts/filters reporting + genres/persons/studios browse
+ the unified /Search/Hints endpoint Swiftfin uses.
"""
import asyncio
import logging

from starlette.responses import JSONResponse

from stash_jellyfin_proxy import runtime
from stash_jellyfin_proxy.mapping.image_policy import performer_item_type
from stash_jellyfin_proxy.stash.client import stash_query
from stash_jellyfin_proxy.stash.query_helpers import get_stash_sort_params, scene_filter_clause_for_parent

logger = logging.getLogger("stash-jellyfin-proxy")


async def endpoint_items_counts(request):
    """`GET /Items/Counts` — aggregate counts by Jellyfin item type."""
    try:
        count_q = """query {
            findScenes { count }
            findPerformers { count }
            findStudios { count }
            findMovies { count }
        }"""
        res = await stash_query(count_q)
        data = res.get("data", {})
        return JSONResponse({
            "MovieCount": data.get("findScenes", {}).get("count", 0),
            "SeriesCount": 0,
            "EpisodeCount": 0,
            "ArtistCount": data.get("findPerformers", {}).get("count", 0),
            "ProgramCount": 0,
            "TrailerCount": 0,
            "SongCount": 0,
            "AlbumCount": 0,
            "MusicVideoCount": 0,
            "BoxSetCount": data.get("findMovies", {}).get("count", 0),
            "BookCount": 0,
            "ItemCount": data.get("findScenes", {}).get("count", 0),
        })
    except Exception as e:
        logger.error(f"Error getting item counts: {e}")
        return JSONResponse({"ItemCount": 0})


async def endpoint_items_filters(request):
    """`GET /Items/Filters` — filter-panel options for Swiftfin's filter
    drawer. Per Phase 4 §8.5:

      - Scope to ParentId. Global query for unscoped / root-scenes /
        unknown-prefix parents; aggregate from scenes-under-parent for
        studio / performer / group / tag-item parents.
      - Split tags into Genres + Tags using the genre_mode allow-list
        (mapping.genre.genre_allowed_names). System tags (SERIES /
        FAVORITE / TAG_GROUPS / GENRE_PARENT_TAG / RATING:) stripped.
      - Sort each dimension by scene count desc, cap at FILTER_TAGS_MAX.
      - Year range and OfficialRatings scoped similarly."""
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    try:
        clause, cvars = scene_filter_clause_for_parent(parent_id)
        allowed = await genre_allowed_names()
        excludes = _filter_exclude_set_lower()

        if clause:
            tag_counts, min_year, max_year = await _scoped_filter_data(clause, cvars)
        else:
            tag_counts, min_year, max_year = await _global_filter_data()

        genre_names, tag_names = _split_tag_counts(
            tag_counts, allowed, excludes, runtime.FILTER_TAGS_MAX
        )

        years: list = []
        if min_year and max_year:
            years = list(range(int(max_year), int(min_year) - 1, -1))

        return JSONResponse({
            "Genres": genre_names,
            "Tags": tag_names,
            "OfficialRatings": [runtime.OFFICIAL_RATING],
            "Years": years,
        })
    except Exception as e:
        logger.error(f"Failed to fetch filters for ParentId={parent_id}: {e}")
        return JSONResponse({
            "Genres": [], "Tags": [],
            "OfficialRatings": [runtime.OFFICIAL_RATING], "Years": [],
        })


def _filter_exclude_set_lower() -> set:
    """Tag names never shown in the filter drawer — plumbing markers."""
    out = set()
    if runtime.SERIES_TAG:
        out.add(runtime.SERIES_TAG.strip().lower())
    if runtime.FAVORITE_TAG:
        out.add(runtime.FAVORITE_TAG.strip().lower())
    if runtime.GENRE_PARENT_TAG:
        out.add(runtime.GENRE_PARENT_TAG.strip().lower())
    for tg in runtime.TAG_GROUPS or []:
        out.add(tg.strip().lower())
    return out


async def _global_filter_data():
    """Top-N tags across the whole library, sorted by scene_count.
    Also returns the library-wide min/max scene dates."""
    tags_q = """query {
        findTags(filter: {per_page: 300, sort: "scenes_count", direction: DESC}) {
            tags { name scene_count }
        }
    }"""
    # Null-date scenes sort first under Stash's ASC direction, which would
    # leave min_year as None. NOT {is_missing: "date"} excludes them.
    oldest_q = """query { findScenes(
        scene_filter: {NOT: {is_missing: "date"}},
        filter: {per_page: 1, sort: "date", direction: ASC}
    ) { scenes { date } } }"""
    newest_q = """query { findScenes(filter: {per_page: 1, sort: "date", direction: DESC}) { scenes { date } } }"""

    tags_res, oldest_res, newest_res = await asyncio.gather(
        stash_query(tags_q),
        stash_query(oldest_q),
        stash_query(newest_q),
    )

    tag_counts = {}
    for t in ((tags_res or {}).get("data", {}).get("findTags") or {}).get("tags", []):
        name = (t.get("name") or "").strip()
        count = int(t.get("scene_count") or 0)
        if name and count > 0:
            tag_counts[name] = count

    def _year(res):
        scenes = ((res or {}).get("data", {}).get("findScenes") or {}).get("scenes") or []
        if scenes and scenes[0].get("date"):
            try:
                return int(scenes[0]["date"][:4])
            except (ValueError, TypeError):
                return None
        return None

    return tag_counts, _year(oldest_res), _year(newest_res)


async def _scoped_filter_data(scene_clause: str, scene_vars: dict):
    """Aggregate tag counts + year range from scenes matching scene_clause.

    One GraphQL call pulls every in-scope scene's tag list and date in a
    single query. The single-studio / single-performer / single-group
    libraries are small enough (hundreds to low thousands of scenes) that
    the full aggregation beats chained Stash queries."""
    q = f"""query ScopedFilterData($ids: [ID!]) {{
        findScenes({scene_clause}, filter: {{per_page: -1}}) {{
            scenes {{ date tags {{ name }} }}
        }}
    }}"""
    res = await stash_query(q, scene_vars)
    scenes = ((res or {}).get("data", {}).get("findScenes") or {}).get("scenes", []) or []

    tag_counts: dict = {}
    min_year = None
    max_year = None
    for s in scenes:
        date = s.get("date")
        if date:
            try:
                yr = int(date[:4])
                if min_year is None or yr < min_year:
                    min_year = yr
                if max_year is None or yr > max_year:
                    max_year = yr
            except (ValueError, TypeError):
                pass
        for t in s.get("tags") or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            tag_counts[name] = tag_counts.get(name, 0) + 1
    return tag_counts, min_year, max_year


def _split_tag_counts(tag_counts: dict, allowed, excludes: set, cap: int):
    """Return (genres, residual_tags) for the filter-panel response.

    Two-pass ordering:
      1. SELECT — walk `tag_counts` in scene-count-desc order so when we
         cap at `cap`, the long tail of rarely-used tags is the part
         dropped (filter panels on large libraries stay useful).
      2. DISPLAY — sort the survivors alphabetically (case-insensitive)
         so the user scans a predictable list rather than a count-desc
         rank that keeps shuffling as scenes get added.

    `allowed` is a lowercase frozenset from genre_allowed_names;
    None means "every non-system tag is a genre" (all_tags mode)."""
    genres: list = []
    residual: list = []
    for name, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0].lower())):
        lc = name.lower()
        if lc in excludes or lc.startswith("rating:"):
            continue
        if allowed is None or lc in allowed:
            genres.append(name)
        else:
            residual.append(name)
    genres = sorted(genres[:cap], key=str.lower)
    residual = sorted(residual[:cap], key=str.lower)
    return genres, residual


# Lazy import so genre module's stash-client dependency isn't pulled in at
# import time (test envs without httpx need to import this module).
async def genre_allowed_names():
    from stash_jellyfin_proxy.mapping.genre import genre_allowed_names as _g
    return await _g()


async def endpoint_genres(request):
    """`GET /Genres` and `/MusicGenres` — Stash tags as Genre items.
    Optionally filtered to only those present in scenes under `parentId`."""
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    try:
        filter_clause, fvars = scene_filter_clause_for_parent(parent_id)

        if filter_clause:
            q = f"""query FindSceneTags($ids: [ID!]) {{
                findScenes({filter_clause}, filter: {{per_page: -1}}) {{
                    scenes {{ tags {{ id name }} }}
                }}
            }}"""
            res = await stash_query(q, fvars)
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            seen = {}
            for s in scenes:
                for t in s.get("tags", []):
                    seen[t["id"]] = t["name"]
            items = [
                {"Name": name, "Id": f"genre-{tid}", "ServerId": runtime.SERVER_ID,
                 "Type": "Genre",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for tid, name in sorted(seen.items(), key=lambda x: x[1])
            ]
        else:
            q = """query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) {
                tags { id name scene_count }
            }}"""
            res = await stash_query(q)
            tags = res.get("data", {}).get("findTags", {}).get("tags", [])
            items = [
                {"Name": t["name"], "Id": f"genre-{t['id']}", "ServerId": runtime.SERVER_ID,
                 "Type": "Genre",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for t in tags if t.get("scene_count", 0) > 0
            ]
        return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})
    except Exception as e:
        logger.error(f"Error getting genres: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_persons(request):
    """`GET /Persons` — performers as Jellyfin Person items with search
    and IsFavorite filter support. Gated by runtime.SEARCH_INCLUDE_PERFORMERS
    when the request is a search (has SearchTerm)."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or runtime.DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, runtime.MAX_PAGE_SIZE))

    # Distinguish "param absent" (library browse → all performers) from
    # "param present but empty" (search view with empty text → no matches).
    # Swiftfin's search view fires /Persons?searchTerm= while a genre is
    # selected; without this gate the People rail lists every performer
    # regardless of the active genre filter.
    has_search_param = any(k.lower() == "searchterm" for k in request.query_params.keys())
    raw_search = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm") or ""
    search_term = raw_search.strip()

    if has_search_param and not search_term:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    # Respect search_include_performers only when the call is a search —
    # /Persons is also used for the Persons *library* browse which should
    # never be gated.
    if search_term and not runtime.SEARCH_INCLUDE_PERFORMERS:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})
    filters_param = request.query_params.get("Filters") or request.query_params.get("filters") or ""
    filter_favorites = "isfavorite" in filters_param.lower()
    folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

    try:
        page = (start_index // limit) + 1

        if search_term:
            clean_search = search_term.strip('"\'')
            logger.debug(f"Persons search: '{clean_search}'")
            count_q = """query CountPerformers($q: String!) { findPerformers(filter: {q: $q}) { count } }"""
            count_res = await stash_query(count_q, {"q": clean_search})
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
            q = """query FindPerformers($q: String!, $page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(filter: {q: $q, page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = await stash_query(q, {"q": clean_search, "page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            logger.debug(f"Persons search '{clean_search}' returned {total_count} matches")
        elif filter_favorites:
            count_q = """query { findPerformers(performer_filter: {filter_favorites: true}) { count } }"""
            count_res = await stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
            q = """query FindFavPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(
                    performer_filter: {filter_favorites: true},
                    filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}
                ) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
            logger.debug(f"Persons favorites returned {total_count} favorite performers")
        else:
            count_q = """query { findPerformers { count } }"""
            count_res = await stash_query(count_q)
            total_count = count_res.get("data", {}).get("findPerformers", {}).get("count", 0)
            q = """query FindPerformers($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
                findPerformers(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                    performers { id name image_path scene_count }
                }
            }"""
            res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})

        performers = res.get("data", {}).get("findPerformers", {}).get("performers", [])
        item_type = performer_item_type(request)
        items = []
        for p in performers:
            item = {
                "Name": p["name"],
                "Id": f"performer-{p['id']}",
                "ServerId": runtime.SERVER_ID,
                "Type": item_type,
                "ImageTags": {"Primary": "img"},
                "ImageBlurHashes": {"Primary": {"img": "000000"}},
                "BackdropImageTags": [],
            }
            if p.get("scene_count") is not None:
                item["ChildCount"] = p["scene_count"]
            items.append(item)
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting persons: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_studios(request):
    """`GET /Studios` — studios as Jellyfin items, optionally filtered to
    only those present in scenes under `parentId`."""
    start_index = max(0, int(request.query_params.get("startIndex") or request.query_params.get("StartIndex") or 0))
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or runtime.DEFAULT_PAGE_SIZE)
    limit = max(1, min(limit, runtime.MAX_PAGE_SIZE))
    parent_id = request.query_params.get("ParentId") or request.query_params.get("parentId")
    folder_sort, folder_dir = get_stash_sort_params(request, context="folders")

    try:
        filter_clause, fvars = scene_filter_clause_for_parent(parent_id)
        if filter_clause:
            q = f"""query FindSceneStudios($ids: [ID!]) {{
                findScenes({filter_clause}, filter: {{per_page: -1}}) {{
                    scenes {{ studio {{ id name image_path }} }}
                }}
            }}"""
            res = await stash_query(q, fvars)
            scenes = res.get("data", {}).get("findScenes", {}).get("scenes", [])
            seen = {}
            for s in scenes:
                studio = s.get("studio")
                if studio:
                    seen[studio["id"]] = studio
            items = [
                {"Name": s["name"], "Id": f"studio-{s['id']}", "ServerId": runtime.SERVER_ID,
                 "Type": "Studio",
                 "ImageTags": {"Primary": "img"},
                 "ImageBlurHashes": {"Primary": {"img": "000000"}},
                 "BackdropImageTags": []}
                for s in sorted(seen.values(), key=lambda x: x["name"])
            ]
            return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})

        count_q = """query { findStudios { count } }"""
        count_res = await stash_query(count_q)
        total_count = count_res.get("data", {}).get("findStudios", {}).get("count", 0)
        page = (start_index // limit) + 1
        q = """query FindStudios($page: Int!, $per_page: Int!, $sort: String!, $direction: SortDirectionEnum!) {
            findStudios(filter: {page: $page, per_page: $per_page, sort: $sort, direction: $direction}) {
                studios { id name image_path scene_count }
            }
        }"""
        res = await stash_query(q, {"page": page, "per_page": limit, "sort": folder_sort, "direction": folder_dir})
        studios = res.get("data", {}).get("findStudios", {}).get("studios", [])
        items = [
            {"Name": s["name"], "Id": f"studio-{s['id']}", "ServerId": runtime.SERVER_ID,
             "Type": "Studio",
             "ImageTags": {"Primary": "img"},
             "ImageBlurHashes": {"Primary": {"img": "000000"}},
             "BackdropImageTags": []}
            for s in studios
        ]
        return JSONResponse({"Items": items, "TotalRecordCount": total_count, "StartIndex": start_index})
    except Exception as e:
        logger.error(f"Error getting studios: {e}")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_search_hints(request):
    """`GET /Search/Hints` — Swiftfin's unified search. Returns typed
    SearchHints for scenes + performers interleaved."""
    search_term = request.query_params.get("searchTerm") or request.query_params.get("SearchTerm") or ""
    limit = int(request.query_params.get("limit") or request.query_params.get("Limit") or 20)
    limit = max(1, min(limit, 50))

    include_item_types_raw = [v for k, v in request.query_params.multi_items() if k.lower() == "includeitemtypes"]
    include_item_types = []
    for val in include_item_types_raw:
        include_item_types.extend([t.strip().lower() for t in val.split(",") if t.strip()])

    hints = []
    total_count = 0
    if not search_term.strip():
        return JSONResponse({"SearchHints": [], "TotalRecordCount": 0})

    clean_search = search_term.strip('"\'')
    # Client IncludeItemTypes filter intersected with runtime search-scope
    # toggles (§8.5). If SEARCH_INCLUDE_SCENES is off, scene hits are
    # dropped even when the client asked for Movie/Video.
    search_scenes = (
        (not include_item_types or "movie" in include_item_types or "video" in include_item_types)
        and runtime.SEARCH_INCLUDE_SCENES
    )
    search_persons = (
        (not include_item_types or "person" in include_item_types)
        and runtime.SEARCH_INCLUDE_PERFORMERS
    )

    try:
        if search_scenes:
            q = """query FindScenes($q: String!, $per_page: Int!) {
                findScenes(filter: {q: $q, per_page: $per_page, sort: "date", direction: DESC}) {
                    count
                    scenes { id title date files { duration } }
                }
            }"""
            res = await stash_query(q, {"q": clean_search, "per_page": limit})
            data = res.get("data", {}).get("findScenes", {})
            total_count += data.get("count", 0)
            for s in data.get("scenes", []):
                scene_id = f"scene-{s['id']}"
                duration = 0
                if s.get("files"):
                    duration = s["files"][0].get("duration") or 0
                title = s.get("title") or f"Scene {s['id']}"
                hint = {
                    "Name": title,
                    "Id": scene_id,
                    "ServerId": runtime.SERVER_ID,
                    "Type": "Movie",
                    "MediaType": "Video",
                    "RunTimeTicks": int(duration * 10000000),
                    "PrimaryImageTag": "img",
                    "ImageTag": "img",
                }
                date = s.get("date")
                if date:
                    hint["ProductionYear"] = int(date[:4])
                hints.append(hint)

        if search_persons:
            perf_limit = max(5, limit // 2)
            q = """query FindPerformers($q: String!, $per_page: Int!) {
                findPerformers(filter: {q: $q, per_page: $per_page}) {
                    count
                    performers { id name image_path }
                }
            }"""
            res = await stash_query(q, {"q": clean_search, "per_page": perf_limit})
            data = res.get("data", {}).get("findPerformers", {})
            total_count += data.get("count", 0)
            for p in data.get("performers", []):
                hint = {
                    "Name": p["name"],
                    "Id": f"performer-{p['id']}",
                    "ServerId": runtime.SERVER_ID,
                    "Type": performer_item_type(request),
                    "MediaType": "",
                }
                if p.get("image_path"):
                    hint["PrimaryImageTag"] = "img"
                hints.append(hint)
    except Exception as e:
        logger.error(f"Search hints error: {e}")

    logger.debug(f"SearchHints '{clean_search}' -> {len(hints)} hints (total={total_count})")
    return JSONResponse({"SearchHints": hints, "TotalRecordCount": total_count})
