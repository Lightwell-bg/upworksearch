"""Parsing of Upwork search pages (job cards) and job pages from saved HTML.

Upwork changes its markup regularly, so every field is extracted in layers:

1. elements marked with known ``data-test`` / ``data-testid`` / ``data-qa`` / ``data-cy``
   tokens (several generations of the markup are listed);
2. text patterns over the card text ("Hourly: $25-$50", "Proposals: 5 to 10",
   "Payment verified", "$10K+ spent", "Rating is 4.9 out of 5", …).

Cards are located from job links, not from tile markup: each job link is expanded to the
largest ancestor that contains no *other* job link, which yields one card per job whatever
tile element Upwork uses. The job title and description are excluded from the text used for
metadata, so "budget $50" inside a description is never taken as the job budget.

A field that cannot be found stays ``None`` — nothing is guessed.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup, Tag

from .htmltext import block_text, flat, inline_text, soup_of
from .ids import job_id_from_uid, job_url, normalize_job_id
from .models import Job

log = logging.getLogger(__name__)

_TEST_ATTRS = ("data-test", "data-testid", "data-qa", "data-cy")
_TILE_TOKENS = frozenset({"jobtile", "job-tile", "job-tile-list", "job-card", "jobs-list-item", "job-feed-tile"})
_STOP_CLIMB = frozenset({"body", "html", "main", "[document]"})

# --- field locators (lower-cased tokens, most specific first) ------------------------------
T_DESCRIPTION = ("job-description-text", "jobdescription", "job-description", "description")
T_SKILLS = ("token", "attr-item", "skill", "jobattrs-token")
T_TYPE = ("job-type", "job-type-label", "is-fixed-price", "budget", "budgetamount")
T_EXPERIENCE = ("contractor-tier", "experience-level", "contractortier")
T_DURATION = ("duration", "duration-label", "est-time", "engagement")
T_POSTED = ("posted-on", "job-pubilshed-date", "job-published-date", "posted-date", "postedon")
T_PROPOSALS = ("proposals", "proposals-tier", "proposals-count")
T_PAYMENT = ("payment-verified", "payment-verification-status", "payment-unverified", "payment-status")
T_RATING = ("total-feedback", "client-feedback", "rating", "buyer-rating", "client-rating", "feedback")
T_SPEND = ("total-spent", "client-spendings", "client-spend", "client-total-spent")
T_COUNTRY = ("client-country", "client-location", "location")

# --- text patterns -----------------------------------------------------------------------
_MONEY = r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?"
_DASH = r"\s*(?:-|–|—|to)\s*"
HOURLY_RANGE_RE = re.compile(r"\bHourly\b[^$\n]{0,24}" + _MONEY + _DASH + _MONEY, re.I)
RANGE_HOURLY_RE = re.compile(_MONEY + _DASH + _MONEY + r"\s*(?:/\s*hr\.?)?\s*Hourly\b", re.I)
HOURLY_SINGLE_RE = re.compile(r"\bHourly\b\s*:?\s*" + _MONEY, re.I)
SINGLE_HOURLY_RE = re.compile(_MONEY + r"\s*(?:/\s*hr\.?)?\s*Hourly\b", re.I)
HOURLY_WORD_RE = re.compile(r"\bHourly\b", re.I)
BUDGET_RE = re.compile(r"(?:Est\.?\s*budget|Budget)\s*:?\s*" + _MONEY, re.I)
FIXED_MONEY_RE = re.compile(r"\bFixed[\s-]*price\b[^$\n]{0,30}\n?\s*" + _MONEY, re.I)
MONEY_FIXED_RE = re.compile(_MONEY + r"\s*Fixed[\s-]*price\b", re.I)
FIXED_WORD_RE = re.compile(r"\bFixed[\s-]*price\b|\bFixed\b", re.I)
EXPERIENCE_RE = re.compile(r"\b(Entry[\s-]*level|Intermediate|Expert)\b", re.I)
DURATION_RE = re.compile(
    r"((?:Less than (?:1|one|a) (?:week|month)|\d+ to \d+ months|More than 6 months|Hours to be determined)"
    r"(?:\s*,\s*(?:Less than 30 hrs/week|30\+ hrs/week|More than 30 hrs/week|Hours to be determined))?)",
    re.I,
)
_REL = r"(?:\d+|an?|one)\s+(?:second|minute|hour|day|week|month|year)s?\s+ago|yesterday|just now|last (?:week|month)"
_ABS = r"[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}"
POSTED_RE = re.compile(rf"Posted\s*:?\s*({_REL}|{_ABS})", re.I)
RELATIVE_RE = re.compile(rf"({_REL})", re.I)
PROPOSALS_RE = re.compile(r"Proposals\s*:?\s*(Less than \d+|\d+\s*to\s*\d+|\d+\s*\+|\d+)", re.I)
PROPOSALS_VALUE_RE = re.compile(r"(Less than \d+|\d+\s*to\s*\d+|\d+\s*\+)", re.I)
PAYMENT_RE = re.compile(r"Payment\s+(?:method\s+)?(verified|unverified|not verified)", re.I)
RATING_OUT_OF_RE = re.compile(r"Rating is\s*(\d(?:\.\d+)?)\s*out of\s*5", re.I)
RATING_REVIEWS_RE = re.compile(r"\b(\d(?:\.\d{1,2})?)\s*(?:of|out of)\s*[\d,]+\s*reviews?", re.I)
RATING_NUMBER_RE = re.compile(r"^\s*(\d(?:\.\d{1,2})?)\s*$")
SPEND_RE = re.compile(_MONEY + r"\s*\+?\s*(?:total\s+)?spent\b", re.I)
_SKILL_NOISE_RE = re.compile(r"^(?:\+\s*\d+|\d+\s*more|more|show more|see more|skills?)$", re.I)
_NOT_A_COUNTRY = frozenset({"worldwide", "anywhere", "remote", "location"})


# ------------------------------------------------------------------------------------------
# Small helpers

def _is_ancestor(ancestor: Tag, node: Tag) -> bool:
    return any(p is ancestor for p in node.parents)


def _inside_any(node: Tag, containers: Sequence[Tag]) -> bool:
    return any(c is node or _is_ancestor(c, node) for c in containers)


def _outermost(elements: list[Tag]) -> list[Tag]:
    return [el for el in elements if not any(o is not el and _is_ancestor(o, el) for o in elements)]


def _money(amount: str, suffix: str | None) -> float:
    value = float(amount.replace(",", ""))
    if suffix:
        value *= 1_000_000 if suffix.lower() == "m" else 1_000
    return value


def _clean(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    return text or None


class _Index:
    """Descendants of ``root`` indexed by test-attribute tokens; ``skip`` subtrees excluded."""

    def __init__(self, root: Tag, skip: Sequence[Tag] = ()):
        self._by_token: dict[str, list[Tag]] = {}
        skip_ids = {id(s) for s in skip}

        def visit(el: Tag) -> None:
            if id(el) in skip_ids:
                return
            for attr in _TEST_ATTRS:
                value = el.get(attr)
                if not value:
                    continue
                if isinstance(value, list):
                    value = " ".join(value)
                for token in str(value).split():
                    self._by_token.setdefault(token.lower(), []).append(el)
            for child in el.children:
                if isinstance(child, Tag):
                    visit(child)

        visit(root)

    def all(self, tokens: Iterable[str]) -> list[Tag]:
        out: list[Tag] = []
        for token in tokens:
            for el in self._by_token.get(token, []):
                if not any(el is seen for seen in out):
                    out.append(el)
        return out

    def first(self, tokens: Iterable[str]) -> Tag | None:
        found = self.all(tokens)
        return found[0] if found else None

    def text(self, tokens: Iterable[str]) -> str:
        """Text of the outermost matching elements, one element per line."""
        return "\n".join(t for t in (block_text(el) for el in _outermost(self.all(tokens))) if t)


# ------------------------------------------------------------------------------------------
# Field parsers (public ones are unit-tested directly)

def parse_budget(text: str) -> tuple[str | None, float | None, float | None, float | None, str | None]:
    """(job_type, fixed_budget, hourly_min, hourly_max, budget_text) from card text."""
    for regex in (HOURLY_RANGE_RE, RANGE_HOURLY_RE):
        m = regex.search(text)
        if m:
            lo, hi = _money(m.group(1), m.group(2)), _money(m.group(3), m.group(4))
            return "hourly", None, min(lo, hi), max(lo, hi), flat(m.group(0))
    for regex in (HOURLY_SINGLE_RE, SINGLE_HOURLY_RE):
        m = regex.search(text)
        if m:
            rate = _money(m.group(1), m.group(2))
            return "hourly", None, rate, rate, flat(m.group(0))
    if HOURLY_WORD_RE.search(text):
        return "hourly", None, None, None, "Hourly"
    for regex in (BUDGET_RE, FIXED_MONEY_RE, MONEY_FIXED_RE):
        m = regex.search(text)
        if m:
            return "fixed", _money(m.group(1), m.group(2)), None, None, flat(m.group(0))
    if FIXED_WORD_RE.search(text):
        return "fixed", None, None, None, "Fixed-price"
    return None, None, None, None, None


def parse_proposals(text: str | None, labeled: bool = True) -> tuple[str | None, int | None, int | None]:
    """"Less than 5" → (0, 4); "5 to 10" → (5, 10); "50+" → (50, None); "12" → (12, 12).

    With ``labeled=True`` the value must follow "Proposals:"; ``labeled=False`` is only for
    the text of a dedicated proposals element (otherwise "1 to 3 months" would match)."""
    if not text:
        return None, None, None
    m = PROPOSALS_RE.search(text)
    if not m and not labeled:
        m = PROPOSALS_VALUE_RE.search(text)
    if not m:
        return None, None, None
    raw = flat(m.group(1))
    low = raw.lower()
    numbers = [int(x) for x in re.findall(r"\d+", low)]
    if low.startswith("less than"):
        return raw, 0, max(numbers[0] - 1, 0)
    if " to " in f" {low} " and len(numbers) >= 2:
        return raw, min(numbers[:2]), max(numbers[:2])
    if "+" in low:
        return raw, numbers[0], None
    return raw, numbers[0], numbers[0]


def estimate_posted_at(text: str | None, now: datetime) -> datetime | None:
    """Absolute time estimate for "3 hours ago", "yesterday", "Sep 20, 2026", …"""
    if not text:
        return None
    low = text.lower().strip()
    if "just now" in low or "second" in low:
        return now
    if "yesterday" in low:
        return now - timedelta(days=1)
    if "last week" in low:
        return now - timedelta(days=7)
    if "last month" in low:
        return now - timedelta(days=30)
    m = re.search(r"(\d+|an?|one)\s+(minute|hour|day|week|month|year)s?\s+ago", low)
    if m:
        n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        unit_days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
        return now - timedelta(days=n * unit_days)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _posted(idx: _Index, meta: str) -> str | None:
    element_text = idx.text(T_POSTED)
    for source in (element_text, meta):
        m = POSTED_RE.search(source or "")
        if m:
            return flat(m.group(1))
    m = RELATIVE_RE.search(element_text or "")
    return flat(m.group(1)) if m else None


def _experience(idx: _Index, meta: str) -> str | None:
    for source in (idx.text(T_EXPERIENCE), meta):
        m = EXPERIENCE_RE.search(source or "")
        if m:
            value = m.group(1).lower()
            return "Entry level" if value.startswith("entry") else value.capitalize()
    return None


def _duration(idx: _Index, meta: str) -> str | None:
    for source in (idx.text(T_DURATION), meta):
        m = DURATION_RE.search(flat(source or ""))
        if m:
            return flat(m.group(1))
    return None


def _proposals(idx: _Index, meta: str) -> tuple[str | None, int | None, int | None]:
    element_text = idx.text(T_PROPOSALS)
    result = parse_proposals(element_text, labeled=False) if element_text else (None, None, None)
    return result if result[0] else parse_proposals(meta, labeled=True)


def _payment(idx: _Index, meta: str) -> bool | None:
    if idx.first(("payment-unverified",)) is not None:
        return False
    for source in (idx.text(T_PAYMENT), meta):
        m = PAYMENT_RE.search(source or "")
        if m:
            return m.group(1).lower() == "verified"
    return None


def _rating(idx: _Index, meta: str) -> float | None:
    """Client rating in (0, 5]. Upwork shows 0 stars for clients without reviews → None."""
    rating_text = idx.text(T_RATING)
    candidates: list[str] = []
    for regex in (RATING_OUT_OF_RE, RATING_REVIEWS_RE):
        for source in (rating_text, meta):
            m = regex.search(source or "")
            if m:
                candidates.append(m.group(1))
    for el in idx.all(T_RATING):
        for value_el in el.select(".air3-rating-value-text, [class*='rating-value']"):
            candidates.append(inline_text(value_el))
    for line in rating_text.splitlines():
        m = RATING_NUMBER_RE.match(line)
        if m:
            candidates.append(m.group(1))
    for raw in candidates:
        try:
            value = float(raw)
        except ValueError:
            continue
        if 0 < value <= 5:
            return round(value, 2)
    return None


def _spend(idx: _Index, meta: str) -> tuple[float | None, str | None]:
    for source in (idx.text(T_SPEND), meta):
        m = SPEND_RE.search(flat(source or ""))
        if m:
            text = re.sub(r"\s*(?:total\s+)?spent$", "", flat(m.group(0)), flags=re.I)
            return _money(m.group(1), m.group(2)), text
    return None, None


def _country(idx: _Index) -> str | None:
    for el in idx.all(T_COUNTRY):
        # <strong>Australia</strong><span>Sydney 7:15 PM</span>: the emphasised part first.
        candidates = [inline_text(s) for s in el.find_all(["strong", "b"])] + block_text(el).splitlines()
        for line in candidates:
            value = re.sub(r"^\s*Location\s*:?\s*", "", line, flags=re.I).strip()
            if value and len(value) <= 60 and not re.search(r"[\d$]", value) and value.lower() not in _NOT_A_COUNTRY:
                return value
    return None


def _skills(root: Tag, idx: _Index, skip: Sequence[Tag]) -> list[str]:
    elements = idx.all(T_SKILLS) + [
        el for el in root.select(".air3-token, .up-skill-badge") if not _inside_any(el, skip)
    ]
    skills: list[str] = []
    seen: set[str] = set()
    for el in elements:
        # Containers holding several tokens (e.g. data-test="TokenClamp JobAttrs") are skipped.
        if any(child is not el for child in el.select("[data-test~='token'], .air3-token")):
            continue
        text = inline_text(el)
        if not text or len(text) > 50 or _SKILL_NOISE_RE.match(text):
            continue
        if text.casefold() not in seen:
            seen.add(text.casefold())
            skills.append(text)
    return skills


_DESCRIPTION_LABELS = frozenset({"description", "job description", "summary"})


def _description_elements(idx: _Index) -> list[Tag]:
    found = _outermost(idx.all(T_DESCRIPTION))
    return [el for el in found if block_text(el) and block_text(el).lower() not in _DESCRIPTION_LABELS]


def _fallback_description(root: Tag, skip: Sequence[Tag]) -> Tag | None:
    """Longest paragraph-like block when no description marker exists."""
    best, best_len = None, 0
    for el in root.find_all(["p", "span", "div"]):
        if _inside_any(el, skip) or any(_is_ancestor(el, s) for s in skip):
            continue
        if el.name != "p" and el.find(["p", "div", "ul", "ol", "a", "button", "li"]) is not None:
            continue
        n = len(block_text(el))
        if n >= 100 and n > best_len:
            best, best_len = el, n
    return best


def _extract(job: Job, root: Tag, title_el: Tag | None, now: datetime, skip: Sequence[Tag] = ()) -> Job:
    """Fill ``job`` from ``root`` (a card or a job page); ``skip`` = foreign blocks."""
    idx = _Index(root, skip)
    desc_elements = [el for el in _description_elements(idx) if not (title_el is not None and _inside_any(title_el, [el]))]
    if not desc_elements:
        fallback = _fallback_description(root, [*skip, *([title_el] if title_el is not None else [])])
        desc_elements = [fallback] if fallback is not None else []
    if desc_elements and not job.description:
        job.description = "\n".join(block_text(el) for el in desc_elements).strip() or None

    excluded = [*skip, *desc_elements, *([title_el] if title_el is not None else [])]
    meta = block_text(root, skip=excluded)

    type_text = idx.text(T_TYPE)
    parsed = parse_budget(type_text) if type_text else (None, None, None, None, None)
    job_type, budget, hmin, hmax, budget_text = parsed
    incomplete = job_type is None or (job_type == "fixed" and budget is None) or (job_type == "hourly" and hmin is None)
    if incomplete:
        t2, b2, lo2, hi2, text2 = parse_budget(meta)
        if job_type is None:
            job_type, budget, hmin, hmax, budget_text = t2, b2, lo2, hi2, text2
        elif t2 == job_type and (b2 is not None or lo2 is not None):
            budget, hmin, hmax, budget_text = b2, lo2, hi2, text2
    job.job_type, job.budget, job.hourly_min, job.hourly_max, job.budget_text = job_type, budget, hmin, hmax, budget_text

    job.skills = job.skills or _skills(root, idx, skip)
    job.posted_text = _posted(idx, meta)
    posted_at = estimate_posted_at(job.posted_text, now)
    job.posted_at = posted_at.isoformat(timespec="seconds") if posted_at else job.posted_at
    job.experience_level = _experience(idx, meta)
    job.duration = _duration(idx, meta)
    job.proposals_text, job.proposals_min, job.proposals_max = _proposals(idx, meta)
    job.payment_verified = _payment(idx, meta)
    job.client_rating = _rating(idx, meta)
    job.client_spend, job.client_spend_text = _spend(idx, meta)
    job.client_country = _country(idx)
    return job


# ------------------------------------------------------------------------------------------
# Search pages

def _link_job_ids(el: Tag) -> set[str]:
    ids = {normalize_job_id(a["href"]) for a in el.find_all("a", href=True)}
    ids.discard(None)
    return ids  # type: ignore[return-value]


def _is_tile(el: Tag) -> bool:
    if el.get("data-ev-sublocation") == "job_feed_tile" or el.name == "article":
        return True
    for attr in _TEST_ATTRS:
        value = el.get(attr)
        if value and {t.lower() for t in str(value).split()} & _TILE_TOKENS:
            return True
    return "job-tile" in (el.get("class") or [])


def _card_for(anchor: Tag, job_id: str) -> Tag:
    """Largest ancestor of ``anchor`` without links to other jobs (stops at a known tile)."""
    best = anchor
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in _STOP_CLIMB:
            break
        if _link_job_ids(parent) - {job_id}:
            break
        best = parent
        if _is_tile(parent):
            break
    return best


def _title_element(card: Tag, job_id: str) -> Tag | None:
    anchors = [a for a in card.find_all("a", href=True) if normalize_job_id(a["href"]) == job_id and inline_text(a)]
    if not anchors:
        return card.find(["h2", "h3", "h4"])
    in_heading = [a for a in anchors if a.find_parent(["h1", "h2", "h3", "h4"]) is not None]
    return (in_heading or sorted(anchors, key=lambda a: -len(inline_text(a))))[0]


def parse_search_page(html: str, now: datetime | None = None) -> list[Job]:
    """All job cards on a search/feed page, in page order, one per job ID."""
    now = now or datetime.now(timezone.utc)
    soup = soup_of(html)
    anchors: dict[str, Tag] = {}
    for a in soup.find_all("a", href=True):
        jid = normalize_job_id(a["href"])
        if jid and jid not in anchors:
            anchors[jid] = a

    cards: list[tuple[str, Tag, Tag | None]] = []
    for jid, anchor in anchors.items():
        card = _card_for(anchor, jid)
        cards.append((jid, card, _title_element(card, jid)))
    # Tiles exposing only a numeric uid (no job link at all) are still cards.
    for tile in soup.find_all(attrs={"data-ev-job-uid": True}):
        jid = job_id_from_uid(tile.get("data-ev-job-uid"))
        if jid and jid not in anchors and not _link_job_ids(tile):
            anchors[jid] = tile
            cards.append((jid, tile, tile.find(["h2", "h3", "h4"])))

    jobs: list[Job] = []
    for jid, card, title_el in cards:
        try:
            job = Job(job_id=jid, url=job_url(jid), title=_clean(inline_text(title_el)) if title_el else None)
            jobs.append(_extract(job, card, title_el, now))
        except Exception:  # one broken card must not lose the others
            log.warning("Не удалось разобрать карточку %s", jid, exc_info=True)
    return jobs


# ------------------------------------------------------------------------------------------
# Job pages

def _json_ld_job(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                kind = item.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    return item
                stack.extend(v for v in item.values() if isinstance(v, (list, dict)))
    return {}


def _foreign_block(anchor: Tag, other_id: str, h1: Tag | None) -> Tag:
    """Small container of a link to another job on a job page ("Similar jobs", …).

    Climbing is bounded (depth, size, never past the page title) so that a lone foreign
    link can never swallow the main job's content or the client block."""
    best = anchor
    for depth, parent in enumerate(anchor.parents):
        if depth >= 4 or not isinstance(parent, Tag) or parent.name in _STOP_CLIMB:
            break
        if _link_job_ids(parent) - {other_id}:
            break
        if h1 is not None and _is_ancestor(parent, h1):
            break
        if len(block_text(parent)) > 800:
            break
        best = parent
        if parent.name in ("li", "article") or _is_tile(parent):
            break
    return best


def parse_job_details(html: str, job_id: str, now: datetime | None = None) -> Job:
    """Fields from an opened job page (other jobs' blocks on the page are excluded)."""
    now = now or datetime.now(timezone.utc)
    soup = soup_of(html)
    job = Job(job_id=job_id, url=job_url(job_id))
    root = soup.find("main") or soup.body or soup
    h1 = root.find("h1")

    foreign: list[Tag] = []
    for a in root.find_all("a", href=True):
        other = normalize_job_id(a["href"])
        if other and other != job_id:
            block = _foreign_block(a, other, h1)
            if not _inside_any(block, foreign):
                foreign = [f for f in foreign if not _is_ancestor(block, f)] + [block]

    ld = _json_ld_job(soup)
    job.title = _clean(inline_text(h1)) if h1 is not None else _clean(ld.get("title"))
    ld_desc = ld.get("description")
    if isinstance(ld_desc, str) and ld_desc.strip():
        job.description = block_text(soup_of(ld_desc)) or None

    _extract(job, root, h1, now, skip=foreign)
    if job.posted_text is None and isinstance(ld.get("datePosted"), str):
        try:
            posted = datetime.fromisoformat(ld["datePosted"].replace("Z", "+00:00"))
            posted = posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)
            job.posted_at = posted.isoformat(timespec="seconds")
            job.posted_text = posted.strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass
    return job
