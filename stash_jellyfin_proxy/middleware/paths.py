"""Case-insensitive request-normalization middleware.

Jellyfin clients disagree on casing for both route segments and query
parameters. Android caps `/Users`, Swiftfin caps `/UserItems`, Infuse
sometimes lower-cases paths, and the Roku Jellyfin channel lower-cases
ALL query parameter names (`parentid=`, `startindex=`, `personids=`).
The framework route table and our handlers both read canonical spellings,
so this middleware normalizes:

  1. `scope["path"]` — every incoming request path is rewritten back to
     the registered route's casing. Dynamic segments (`{user_id}`,
     `{item_id}`) are matched as wildcards and preserve their original
     request value; only static segments get canonical casing.

  2. `scope["query_string"]` — parameter *names* whose lowercase form
     matches a known canonical spelling are rewritten. Parameter values
     are left untouched. Unknown parameters are passed through unchanged.
     Handlers can now read `.get("ParentId")` and get a hit from any
     client casing.

Fixes issue #16 (Infuse lowercase paths) + issue #27 (Roku lowercase
query parameters, credit @madlens95).
"""

from urllib.parse import parse_qsl, urlencode


# Canonical spellings for the query-parameter names read by the proxy's
# handlers. Any incoming param whose lowercase form matches a key here gets
# rewritten to the value before handlers see it — so `parentid`, `parentId`,
# and `ParentId` all reach `.get("ParentId")`. Add a new entry when adding
# an endpoint that reads a new query parameter.
_CANON_PARAMS = {
    "parentid":         "ParentId",
    "startindex":       "StartIndex",
    "limit":            "Limit",
    "ids":              "Ids",
    "personids":        "PersonIds",
    "studioids":        "StudioIds",
    "searchterm":       "SearchTerm",
    "sortby":           "SortBy",
    "sortorder":        "SortOrder",
    "seasonid":         "SeasonId",
    "entryids":         "EntryIds",
    "filters":          "Filters",
    "name":             "Name",
    "includeitemtypes": "IncludeItemTypes",
    # Genre-related — added alongside issue #28 GenreIds resolution.
    "genres":           "Genres",
    "genreids":         "GenreIds",
    "tags":             "Tags",
    "years":            "Years",
}


def _normalize_query_string(qs: bytes) -> bytes:
    """Return `qs` with any parameter name whose lowercase form is in
    _CANON_PARAMS rewritten to its canonical spelling. Returns the input
    unchanged when no rewriting is needed so the middleware can skip the
    scope-copy in the common case."""
    if not qs:
        return qs
    try:
        # latin-1 is safe for query strings — they're URL-encoded ASCII.
        pairs = parse_qsl(qs.decode("latin-1"), keep_blank_values=True)
    except Exception:
        return qs
    rewrote = False
    canon_pairs = []
    for k, v in pairs:
        canon = _CANON_PARAMS.get(k.lower())
        if canon and canon != k:
            canon_pairs.append((canon, v))
            rewrote = True
        else:
            canon_pairs.append((k, v))
    if not rewrote:
        return qs
    return urlencode(canon_pairs).encode("latin-1")


class CaseInsensitivePathMiddleware:
    _static_map = None
    _templates = None

    def __init__(self, app):
        self.app = app

    @classmethod
    def build_path_map(cls, route_list):
        """Build the lookup tables from a Starlette route list. Call once at
        app-construction time with the same route list passed to Starlette."""
        cls._static_map = {}
        cls._templates = []
        for r in route_list:
            p = getattr(r, "path", "")
            if not p:
                continue
            if "{" not in p:
                cls._static_map[p.lower()] = p
            else:
                segments = p.split("/")
                template = []
                for seg in segments:
                    if seg and seg.startswith("{"):
                        template.append(None)
                    else:
                        template.append(seg)
                cls._templates.append((template, p))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Query-string normalization first. Cheap and independent of path
            # matching — even if the path is fine, a Roku request needs its
            # parentid= rewritten to ParentId= before the handler reads it.
            qs = scope.get("query_string", b"")
            new_qs = _normalize_query_string(qs)
            if new_qs is not qs:
                scope = dict(scope, query_string=new_qs)

            path = scope.get("path", "")
            path_lower = path.lower()

            # Try the request path as-is first. Some routes are registered
            # with an explicit trailing slash (e.g. `/Playlists/`), so we
            # must not pre-strip before this lookup.
            rewritten = self._static_map.get(path_lower)

            # Fallback: drop a trailing slash and retry. Roku's Jellyfin
            # client appends `/` on a few collection paths (`/items/?...`).
            if rewritten is None and len(path_lower) > 1 and path_lower.endswith("/"):
                rewritten = self._static_map.get(path_lower[:-1])

            if rewritten is not None:
                scope = dict(scope, path=rewritten)
            else:
                # Always try template matching — even for fully-lowercase
                # paths. Original middleware only fired when path differed
                # from its lowercase form; that missed Roku requests like
                # `/items/scene-11/images` where my new
                # `/Items/{item_id}/Images` route would otherwise never see
                # the request.
                lookup_path = path
                if len(lookup_path) > 1 and lookup_path.endswith("/"):
                    lookup_path = lookup_path[:-1]
                req_segments = lookup_path.split("/")
                req_count = len(req_segments)
                for template, original in self._templates:
                    if len(template) != req_count:
                        continue
                    ok = True
                    for i, t_seg in enumerate(template):
                        if t_seg is None:
                            continue
                        if t_seg.lower() != req_segments[i].lower():
                            ok = False
                            break
                    if ok:
                        rebuilt = []
                        for i, t_seg in enumerate(template):
                            if t_seg is None:
                                rebuilt.append(req_segments[i])
                            else:
                                rebuilt.append(t_seg)
                        scope = dict(scope, path="/".join(rebuilt))
                        break

        await self.app(scope, receive, send)
