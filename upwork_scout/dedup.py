"""Detection of reposted jobs: the same job published again under a new Upwork ID.

Upwork gives a reposted job a fresh ID, so ID-based "newness" alone would show it as new.
Job B is a repost of an earlier job A (seen at most ``dedup.window_days`` before B) when
their client countries do not contradict each other and either

- the normalized title + description are identical, or
- the descriptions overlap strongly — word 3-gram *containment* (shared / smaller set)
  ≥ ``min_similarity``; containment rather than Jaccard because a search card carries only
  a truncated prefix of the full description — and the titles are similar
  (word Jaccard ≥ ``min_title_similarity``).

Texts shorter than ``min_words`` are compared only exactly: a few shared words prove nothing.
"Earlier" is the total order (first_seen_run_id, first_seen_at, job_id), so the result
depends only on the database, not on evaluation order, and a rescore reproduces it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import DedupCfg
from .storage import JobRecord, Storage

_TOKEN = re.compile(r"[0-9a-zа-яё]+")
_SHINGLE = 3


def _words(text: str | None) -> list[str]:
    return _TOKEN.findall((text or "").casefold())


@dataclass(frozen=True)
class _Doc:
    job_id: str
    order: tuple[int, str, str]
    first_seen: datetime
    country: str | None
    exact: str
    title_words: frozenset[str]
    shingles: frozenset[tuple[str, ...]]


def _doc(job_id: str, title: str | None, description: str | None, country: str | None,
         first_seen_run_id: int, first_seen_at: str) -> _Doc:
    title_w, desc_w = _words(title), _words(description)
    body = desc_w or title_w
    return _Doc(
        job_id=job_id,
        order=(first_seen_run_id, first_seen_at, job_id),
        first_seen=datetime.fromisoformat(first_seen_at),
        country=(country or "").strip().casefold() or None,
        exact=" ".join(title_w) + "|" + " ".join(desc_w),
        title_words=frozenset(title_w),
        shingles=frozenset(tuple(body[i:i + _SHINGLE]) for i in range(len(body) - _SHINGLE + 1)),
    )


def _title_similarity(a: _Doc, b: _Doc) -> float:
    if not a.title_words or not b.title_words:
        return 1.0  # a missing title is not evidence against a match
    return len(a.title_words & b.title_words) / len(a.title_words | b.title_words)


def _is_repost(job: _Doc, earlier: _Doc, shared: int, cfg: DedupCfg) -> bool:
    if job.country and earlier.country and job.country != earlier.country:
        return False
    if job.exact == earlier.exact and job.exact != "|":
        return True
    smaller = min(len(job.shingles), len(earlier.shingles))
    if smaller < max(1, cfg.min_words - _SHINGLE + 1):
        return False
    return shared / smaller >= cfg.min_similarity and _title_similarity(job, earlier) >= cfg.min_title_similarity


def find_reposts(store: Storage, cfg: DedupCfg, records: Iterable[JobRecord]) -> dict[str, str]:
    """``{job_id: original_job_id}`` for every record that repeats an earlier job;
    the original is the earliest matching job."""
    records = list(records)
    if not cfg.enabled or not records:
        return {}
    earliest = min(datetime.fromisoformat(r.first_seen_at) for r in records)
    since = earliest - timedelta(days=cfg.window_days + 1)  # +1 day: tolerate mixed UTC offsets
    pool = [_doc(r["job_id"], r["title"], r["description"], r["client_country"],
                 r["first_seen_run_id"], r["first_seen_at"])
            for r in store.dedup_pool(since.isoformat(timespec="seconds"))]
    by_exact: dict[str, list[_Doc]] = defaultdict(list)
    by_shingle: dict[tuple[str, ...], list[_Doc]] = defaultdict(list)
    for d in pool:
        by_exact[d.exact].append(d)
        for sh in d.shingles:
            by_shingle[sh].append(d)
    docs = {d.job_id: d for d in pool}
    window = timedelta(days=cfg.window_days)

    out: dict[str, str] = {}
    for rec in records:
        job = rec.job
        doc = docs.get(job.job_id) or _doc(job.job_id, job.title, job.description, job.client_country,
                                           rec.first_seen_run_id, rec.first_seen_at)
        shared: Counter[str] = Counter()
        for sh in doc.shingles:
            for other in by_shingle.get(sh, ()):
                shared[other.job_id] += 1
        candidates = {d.job_id: d for d in by_exact.get(doc.exact, ())}
        candidates.update((jid, docs[jid]) for jid in shared)
        matches = [
            c for c in candidates.values()
            if c.order < doc.order
            and doc.first_seen - c.first_seen <= window
            and _is_repost(doc, c, shared[c.job_id], cfg)
        ]
        if matches:
            out[job.job_id] = min(matches, key=lambda c: c.order).job_id
    return out
