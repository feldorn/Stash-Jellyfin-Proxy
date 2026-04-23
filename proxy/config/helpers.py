"""Pure helpers for config-value coercion and normalization."""
import uuid


def parse_bool(value, default=True):
    """Parse a boolean value from a config string.
    Accepts: true/yes/1/on (case-insensitive) → True; anything else → False.
    Non-string, non-bool inputs fall back to `default`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ('true', 'yes', '1', 'on')
    return default


def normalize_path(path, default="/graphql"):
    """Normalize a URL path: ensure leading /, strip trailing /.
    Empty/whitespace → `default`."""
    if not path or not path.strip():
        return default
    p = path.strip()
    if not p.startswith('/'):
        p = '/' + p
    if len(p) > 1 and p.endswith('/'):
        p = p.rstrip('/')
    return p


def normalize_server_id(server_id):
    """Ensure SERVER_ID is in standard UUID format (8-4-4-4-12 with dashes).
    Converts old dashless 32-char hex IDs to proper UUID format; leaves
    other values alone (so a malformed ID doesn't brick startup — the Web
    UI flags it instead)."""
    clean = server_id.strip().replace("-", "")
    if len(clean) == 32:
        try:
            return str(uuid.UUID(clean))
        except ValueError:
            pass
    return server_id


def generate_server_id():
    """Generate a server ID in standard UUID format (8-4-4-4-12)."""
    return str(uuid.uuid4())
