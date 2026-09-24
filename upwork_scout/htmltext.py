"""Readable text extraction from BeautifulSoup trees.

Inline elements (``<strong>``, ``<span>``) keep text on one line — "Proposals: <strong>5 to
10</strong>" becomes "Proposals: 5 to 10" — while block elements start new lines, which the
field regexes in :mod:`upwork_scout.parsing` rely on.
"""

from __future__ import annotations

from collections.abc import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction

from .ids import normalize_job_id, parse_search_id

_BLOCK_TAGS = frozenset(
    "address article aside blockquote br dd div dl dt footer form h1 h2 h3 h4 h5 h6 header "
    "hr li main nav ol p pre section table tbody td th thead tr ul button".split()
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_NON_TEXT = (Comment, Declaration, Doctype, CData, ProcessingInstruction)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def block_text(root: Tag, skip: Iterable[Tag] = ()) -> str:
    """Text of ``root`` with one line per block; subtrees in ``skip`` are left out."""
    skip_ids = {id(el) for el in skip}
    parts: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Tag):
                if id(child) in skip_ids or child.name in _SKIP_TAGS:
                    continue
                is_block = child.name in _BLOCK_TAGS
                if is_block:
                    parts.append("\n")
                walk(child)
                if is_block:
                    parts.append("\n")
            elif isinstance(child, NavigableString) and not isinstance(child, _NON_TEXT):
                parts.append(str(child))

    if id(root) not in skip_ids:
        walk(root)
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


def inline_text(el: Tag | None) -> str:
    """Single-line text of an element with collapsed whitespace."""
    if el is None:
        return ""
    return " ".join(block_text(el).split())


def flat(text: str) -> str:
    return " ".join(text.split())


# --- diagnostics ---------------------------------------------------------------------------
_DEBUG_DROP_TAGS = (
    "script", "style", "noscript", "template", "iframe", "object", "embed", "form", "input",
    "textarea", "select", "button", "meta", "link", "svg", "img", "picture", "video", "audio",
    "header", "nav", "aside", "footer", "dialog",
)
_DEBUG_KEEP_ATTRS = frozenset(
    {"class", "data-test", "data-testid", "data-qa", "data-cy", "data-ev-sublocation",
     "data-ev-job-uid", "role", "aria-label", "title", "lang"}
)


def sanitize_for_debug(html: str) -> str:
    """Reduce an authenticated page to its visible content structure for markup diagnostics.

    Keeps only ``<main>`` (or ``<body>``), drops scripts, forms, inputs, embedded frames,
    header/nav/aside/footer (account menus), and every attribute outside a small allowlist;
    only links to jobs or saved searches keep their ``href``. Hidden state such as CSRF
    tokens, bootstrap JSON or account data therefore never reaches the debug file."""
    soup = soup_of(html)
    root = soup.find("main") or soup.body or soup
    for el in root.find_all(_DEBUG_DROP_TAGS):
        el.decompose()
    for el in root.find_all(string=lambda s: isinstance(s, Comment)):
        el.extract()
    for el in [root, *root.find_all(True)]:
        href = el.get("href") if el.name == "a" else None
        el.attrs = {k: v for k, v in el.attrs.items() if k in _DEBUG_KEEP_ATTRS}
        if href and (normalize_job_id(href) or parse_search_id(href)):
            el.attrs["href"] = href
    body = root.decode() if root is not soup else "".join(str(c) for c in root.contents)
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>upwork-scout debug</title></head>\n"
        f"<body>\n<!-- sanitised by upwork-scout: scripts, forms, inputs and non-content attributes removed -->\n"
        f"{body}\n</body></html>\n"
    )
