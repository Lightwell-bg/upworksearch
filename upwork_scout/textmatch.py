"""Phrase matching used by the rules: case-insensitive, word-bounded, ``*`` = prefix match.

``"api"`` matches "API" and "REST API" but not "rapid"; ``"automat*"`` matches
"automation" and "automated"; spaces in a phrase also match ``-``, ``_`` and ``/``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

_WORD_CHAR = "0-9a-zа-яё"


@lru_cache(maxsize=4096)
def compile_phrase(phrase: str) -> re.Pattern[str]:
    text = " ".join(phrase.strip().lower().split())
    prefix = text.endswith("*")
    if prefix:
        text = text[:-1].rstrip()
    if not text:
        raise ValueError(f"empty phrase: {phrase!r}")
    body = r"[\s\-_/]+".join(re.escape(tok) for tok in text.split(" "))
    starts_word = bool(re.match(f"[{_WORD_CHAR}]", text))
    ends_word = bool(re.search(f"[{_WORD_CHAR}]$", text))
    lead = f"(?<![{_WORD_CHAR}])" if starts_word else ""
    tail = f"(?![{_WORD_CHAR}])" if ends_word and not prefix else ""
    return re.compile(lead + body + tail, re.IGNORECASE)


def find_phrases(text: str | None, phrases: Iterable[str]) -> list[str]:
    """Phrases (as written in config) that occur in ``text``."""
    if not text:
        return []
    return [p for p in phrases if p.strip() and compile_phrase(p).search(text)]


def contains_any(text: str | None, phrases: Iterable[str]) -> bool:
    return bool(find_phrases(text, phrases))
