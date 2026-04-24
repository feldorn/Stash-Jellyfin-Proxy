"""Title-to-SortName conversion.

Strip a configured list of leading articles ("The", "A", "An", …) so
"The Matrix" sorts under M, not T. Case-insensitive match; original case
of the remaining title is preserved. Punctuation between the article and
the rest (if any) is trimmed.

Reads runtime.SORT_STRIP_ARTICLES for the article list."""
from __future__ import annotations

from stash_jellyfin_proxy import runtime


def sort_name_for(title: str) -> str:
    """Return the SortName for a display title. Empty input returns ''."""
    if not title:
        return ""
    articles = [a.strip() for a in (runtime.SORT_STRIP_ARTICLES or []) if a.strip()]
    if not articles:
        return title
    stripped = title.strip()
    lower = stripped.lower()
    for article in articles:
        art_lc = article.lower()
        if lower.startswith(art_lc + " "):
            remainder = stripped[len(article):].lstrip(" \t-—:.,")
            return remainder or stripped
        # also handle articles followed directly by punctuation (rare)
        if lower.startswith(art_lc) and len(stripped) > len(article) and not stripped[len(article)].isalnum():
            remainder = stripped[len(article):].lstrip(" \t-—:.,")
            if remainder:
                return remainder
    return stripped
