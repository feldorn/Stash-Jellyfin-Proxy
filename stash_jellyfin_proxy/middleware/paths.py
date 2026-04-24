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

            if path_lower in self._static_map:
                scope = dict(scope, path=self._static_map[path_lower])
            elif path_lower != path:
                req_segments = path.split("/")
                req_count = len(req_segments)
                matched = False
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
                        matched = True
                        break
                if not matched:
                    scope = dict(scope, path=path)

        await self.app(scope, receive, send)
