"""Regression: text-file reads must specify UTF-8 explicitly so the
proxy doesn't crash on Windows, where `Path.read_text()` defaults to the
platform encoding (cp1252) — which can't decode the non-ASCII content
the project actually ships (e.g. the eyeball emojis in index.html).

Issue #24: UnicodeDecodeError 'charmap' codec can't decode byte 0x81 in
position 11848 — the dashboard template fails to load on Windows native
runs unless an explicit encoding is supplied.
"""
from pathlib import Path

import pytest


def test_index_html_must_be_decoded_as_utf8():
    """The dashboard template is the canonical site of the issue. Lock
    that:
      1. The file contains bytes that fail cp1252 decoding, so the
         explicit encoding='utf-8' in ui/api.py is load-bearing — anyone
         removing it would re-introduce the crash on Windows.
      2. UTF-8 decoding succeeds and yields the expected content.
    """
    template = (
        Path(__file__).resolve().parents[2]
        / "stash_jellyfin_proxy" / "ui" / "templates" / "index.html"
    )
    assert template.is_file(), "test setup: index.html is missing"

    with pytest.raises(UnicodeDecodeError):
        template.read_text(encoding="cp1252")

    text = template.read_text(encoding="utf-8")
    assert text, "template decoded as empty under utf-8"
    # The eyeball / peek-a-boo emojis on the Connect-a-Player password
    # field are the bytes that triggered the original report.
    assert "\U0001F441" in text or "\U0001F648" in text, (
        "index.html should still contain the eyeball/peek emojis that "
        "make explicit UTF-8 decoding necessary"
    )
