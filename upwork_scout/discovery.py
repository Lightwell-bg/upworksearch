"""Dynamic discovery of saved searches on https://www.upwork.com/nx/find-work/.

A saved search is any link whose path is exactly ``/nx/find-work/{digits}``. Named tabs such
as ``best-matches`` or ``most-recent`` and job links like ``/nx/find-work/123/details/~01…``
do not match, so no IDs or names are hardcoded.
"""

from __future__ import annotations

from .htmltext import inline_text, soup_of
from .ids import parse_search_id, search_url
from .models import SavedSearch

_MAX_NAME_LEN = 120


def _link_name(a) -> str:
    name = inline_text(a)
    if not name:
        for attr in ("aria-label", "title", "data-ev-label"):
            value = a.get(attr)
            if value:
                name = " ".join(str(value).split())
                break
    return name[:_MAX_NAME_LEN]


def parse_saved_searches(html: str) -> list[SavedSearch]:
    """Saved searches in page order, de-duplicated by ID (first non-empty name wins)."""
    soup = soup_of(html)
    names: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        search_id = parse_search_id(a["href"])
        if not search_id:
            continue
        name = _link_name(a)
        if search_id not in names or (not names[search_id] and name):
            names[search_id] = name
    return [
        SavedSearch(search_id=sid, name=name or f"Поиск {sid}", url=search_url(sid))
        for sid, name in names.items()
    ]
