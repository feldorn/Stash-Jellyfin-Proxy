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


def collapse_blank_runs(lines):
    """Collapse runs of 2+ consecutive blank lines into a single blank
    line. Repeated strip-and-reinsert cycles in save_config_value (e.g.
    CONFIG_LAST_BOOT_AT, rewritten every boot) leave behind one extra
    blank per cycle when the stripped key was bracketed by separator
    blanks; this normalizes that drift before write.
    """
    out = []
    prev_blank = False
    for line in lines:
        is_blank = line.strip() == ''
        if is_blank and prev_blank:
            continue
        out.append(line)
        prev_blank = is_blank
    return out


def find_global_insert_idx(lines):
    """Return where to insert a flat global key into `lines`, or None if
    the file has no `[section]` headers (caller should append at end).

    The insertion point is just before the first `[section]` header,
    backed up over any preceding "# ==== ... ====" decorative divider
    so the new key lands above the divider that visually heads the
    upcoming section. Walking is over the whole pre-section range
    (not just contiguous blanks) so misplaced keys above the section
    header don't block the divider search.
    """
    first_section_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('[') and s.endswith(']'):
            first_section_idx = i
            break
    if first_section_idx is None:
        return None
    insertion_idx = first_section_idx
    for i in range(first_section_idx - 1, -1, -1):
        s = lines[i].strip()
        if s.startswith('# ====') and s.endswith('===='):
            insertion_idx = i
            break
    return insertion_idx


def _line_matches_key(line: str, key: str) -> bool:
    """True if `line` is `KEY = ...` or `# KEY = ...`, exact key match.
    Used so SERVER_ID doesn't collide with a hypothetical SERVER_ID_FOO."""
    s = line.strip()
    if s.startswith('#'):
        s = s[1:].lstrip()
    if not s.startswith(key):
        return False
    rest = s[len(key):]
    return rest.lstrip().startswith('=')


def save_config_value(config_file: str, key: str, value: str, comment: str = None) -> bool:
    """Write a KEY = value line at global scope in the config file.

    Removes any prior line for `key` (active or commented, anywhere in
    the file — including inside a `[section]` block) and re-inserts at
    global scope, just before the first `[section]` header. If the file
    has no sections, appends at the end.

    Inserting at global scope matters because the loader puts any
    `KEY = value` line that follows a `[section]` header into that
    section's dict — so a flat key appended below a trailing player
    profile block would be invisible to bootstrap on the next load.
    """
    if not os.path.isfile(config_file):
        with open(config_file, 'w', encoding='utf-8') as f:
            if comment:
                f.write(f'# {comment}\n')
            f.write(f'{key} = {value}\n')
        return True

    with open(config_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Strip every existing line for this key (active or commented),
    # regardless of section. Also strip any prior occurrence of the
    # same comment line we're about to insert — otherwise repeated
    # saves of a per-boot key (CONFIG_LAST_BOOT_AT) accumulate one
    # extra comment copy per boot.
    comment_marker = f'# {comment}'.strip() if comment else None
    cleaned = []
    for line in lines:
        if _line_matches_key(line, key):
            continue
        if comment_marker and line.strip() == comment_marker:
            continue
        cleaned.append(line)

    new_block = []
    if comment:
        new_block.append(f'# {comment}\n')
    new_block.append(f'{key} = {value}\n')

    insertion_idx = find_global_insert_idx(cleaned)
    if insertion_idx is None:
        if cleaned and not cleaned[-1].endswith('\n'):
            cleaned.append('\n')
        if cleaned and cleaned[-1].strip() != '':
            cleaned.append('\n')
        cleaned.extend(new_block)
    else:
        new_block.append('\n')
        cleaned[insertion_idx:insertion_idx] = new_block

    cleaned = collapse_blank_runs(cleaned)
    with open(config_file, 'w', encoding='utf-8') as f:
        f.writelines(cleaned)
    return True


def save_server_id_to_config(config_file: str, server_id: str) -> bool:
    """Convenience wrapper for saving SERVER_ID."""
    return save_config_value(config_file, "SERVER_ID", server_id, "Server identification (auto-generated)")
