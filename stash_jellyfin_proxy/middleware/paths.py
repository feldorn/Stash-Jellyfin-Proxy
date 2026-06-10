"""Case-insensitive path normalization middleware.

Jellyfin clients disagree on casing for route segments (Android caps
`/Users`, Swiftfin caps `/UserItems`, Infuse sometimes lower-cases). The
framework route table is case-sensitive. This middleware normalizes every
incoming request path back to the registered route's casing so handlers
see consistent paths regardless of client conventions.

Dynamic segments (`{user_id}`, `{item_id}`) are matched as wildcards and
preserve their original request value; only the static segments get
rewritten to the route's casing.
"""


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
