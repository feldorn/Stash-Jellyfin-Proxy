"""Pure helpers for config-value coercion, normalization, and persistence."""
import os
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


def save_config_value(config_file: str, key: str, value: str, comment: str = None) -> bool:
    """Write a KEY = value line to the config file. Updates an existing
    entry (commented or active) in-place; appends if not found."""
    if not os.path.isfile(config_file):
        with open(config_file, 'w') as f:
            if comment:
                f.write(f'# {comment}\n')
            f.write(f'{key} = {value}\n')
        return True

    with open(config_file, 'r') as f:
        lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#') and key in stripped and '=' in stripped:
            new_lines.append(f'{key} = {value}\n')
            updated = True
        elif stripped.startswith(key) and '=' in stripped:
            new_lines.append(f'{key} = {value}\n')
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        prefix = f'\n# {comment}\n' if comment else '\n'
        new_lines.append(f'{prefix}{key} = {value}\n')

    with open(config_file, 'w') as f:
        f.writelines(new_lines)
    return True


def save_server_id_to_config(config_file: str, server_id: str) -> bool:
    """Convenience wrapper for saving SERVER_ID."""
    return save_config_value(config_file, "SERVER_ID", server_id, "Server identification (auto-generated)")
