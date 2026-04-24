"""Config file parser — flat KEY=value pairs plus [section.name] blocks.

Grammar:
    # comments and blank lines ignored
    KEY = value                # flat key (always global scope)
    KEY = "quoted value"
    [section.name]             # opens a section scope
    key = value                # scoped into the current section

Pure function, no module-level state. Callers in the monolith hold the
loaded dicts and coordinate the local-override merge.
"""
import os
import sys


def load_config(filepath):
    """Load configuration from a shell-style config file with optional
    INI-style section blocks.

    Returns a 3-tuple:
        config (dict): flat KEY → value for keys in the global scope
        defined_keys (set): flat keys explicitly present in the file
        sections (dict): {section_name: {key: value}} for every [section]
                         block; empty dict if none.
    """
    config = {}
    defined_keys = set()
    sections = {}
    current_section = None  # None = global scope
    if os.path.isfile(filepath):
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue
                    # Section header: [name] opens a new scope for subsequent
                    # key/value lines. Empty or malformed headers reset to
                    # global scope rather than raise — we log to stderr but
                    # keep loading so a partial-bad file doesn't brick startup.
                    if line.startswith('[') and line.endswith(']'):
                        name = line[1:-1].strip()
                        if name:
                            current_section = name
                            sections.setdefault(current_section, {})
                        else:
                            current_section = None
                        continue
                    # KEY=value or KEY="value" — into section or global.
                    if '=' in line:
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if current_section is None:
                            config[key] = value
                            defined_keys.add(key)
                        else:
                            sections[current_section][key] = value
        except Exception as e:
            print(f"Error loading config file {filepath}: {e}", file=sys.stderr)
    return config, defined_keys, sections
