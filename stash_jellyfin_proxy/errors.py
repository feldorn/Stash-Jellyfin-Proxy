"""Typed exceptions for the error-handling contract (plan §4.8).

Endpoints raise these instead of returning error responses directly.
The Starlette exception_handlers in proxy/app.py translate them to
JSON responses so clients always get a parseable body.
"""
import logging

from starlette.responses import JSONResponse

logger = logging.getLogger("stash-jellyfin-proxy")


class StashUnavailable(Exception):
    """Raised when Stash is unreachable (connection refused, timeout)."""


class StashError(Exception):
    """Raised when Stash returned GraphQL errors; detail carries the msg."""


class BadRequest(Exception):
    """Raised when a query param or body field is invalid/un-coercible."""

    def __init__(self, field: str, detail: str = ""):
        super().__init__(detail or field)
        self.field = field
        self.detail = detail or f"invalid value for '{field}'"


def _error_json(status: int, kind: str, **extra):
    payload = {"error": kind}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


async def _stash_unavailable_handler(request, exc):
    logger.error(f"stash_unavailable on {request.method} {request.url.path}: {exc}")
    return _error_json(503, "stash_unavailable")


async def _stash_error_handler(request, exc):
    logger.error(f"stash_error on {request.method} {request.url.path}: {exc}")
    return _error_json(502, "stash_error", detail=str(exc)[:200])


async def _bad_request_handler(request, exc):
    logger.info(f"bad_request on {request.method} {request.url.path}: {exc.field}: {exc.detail}")
    return _error_json(400, "bad_request", field=exc.field, detail=exc.detail)


# Map exception type → handler; consumed by proxy/app.py when building Starlette.
ERROR_CONTRACT_HANDLERS = {
    StashUnavailable: _stash_unavailable_handler,
    StashError: _stash_error_handler,
    BadRequest: _bad_request_handler,
}
