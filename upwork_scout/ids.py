"""Normalisation of Upwork job IDs and saved-search IDs.

Job IDs are Upwork "ciphertexts": ``~01`` + 18 hex chars (older) or ``~02`` + 19 digits
(newer). The canonical form stored everywhere is the ciphertext without ``~``, lowercased,
e.g. ``01a2b3c4d5e6f7a8b9`` — so the same job found through different URL shapes merges.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

UPWORK_BASE = "https://www.upwork.com"
_UPWORK_HOSTS = {"www.upwork.com", "upwork.com"}

_CIPHER = r"0\d[0-9a-z]{14,30}"
# A job ciphertext is only trusted inside a job-shaped path, so profile links such as
# /freelancers/~01… are never mistaken for jobs.
_JOB_HREF_RE = re.compile(
    rf"/(?:jobs|job|details|apply)/(?:[^/?#\s]*_)?~({_CIPHER})(?![0-9a-z])",
    re.IGNORECASE,
)
_BARE_RE = re.compile(rf"^~?({_CIPHER})$", re.IGNORECASE)
_CANON_RE = re.compile(rf"^{_CIPHER}$")
_UID_RE = re.compile(r"^\d{15,25}$")
_SEARCH_PATH_RE = re.compile(r"^/nx/find-work/(\d{1,20})/?$")


def normalize_job_id(value: str | None) -> str | None:
    """Canonical job ID from a job URL/href, ``~01…`` or ``01…``; ``None`` if not a job."""
    if not value:
        return None
    text = str(value).strip()
    m = _JOB_HREF_RE.search(text)
    if m:
        return m.group(1).lower()
    m = _BARE_RE.match(text)
    return m.group(1).lower() if m else None


def job_id_from_uid(uid: str | None) -> str | None:
    """Numeric ``data-ev-job-uid`` → ``02…`` ciphertext (the form Upwork uses in URLs)."""
    if uid and _UID_RE.match(str(uid).strip()):
        return "02" + str(uid).strip()
    return None


def is_valid_job_id(job_id: str) -> bool:
    return bool(job_id) and bool(_CANON_RE.match(job_id))


def job_url(job_id: str) -> str:
    """Direct, safe link to a job. Raises ValueError for anything but a canonical ID."""
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    return f"{UPWORK_BASE}/jobs/~{job_id}"


def parse_search_id(href: str | None) -> str | None:
    """Saved-search ID from ``/nx/find-work/{digits}`` (relative or absolute, upwork.com only)."""
    if not href:
        return None
    absolute = urljoin(UPWORK_BASE + "/", href.strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in _UPWORK_HOSTS:
        return None
    m = _SEARCH_PATH_RE.match(parsed.path)
    return m.group(1) if m else None


def search_url(search_id: str) -> str:
    if not search_id.isdigit():
        raise ValueError(f"invalid search id: {search_id!r}")
    return f"{UPWORK_BASE}/nx/find-work/{search_id}"


def is_upwork_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _UPWORK_HOSTS


def is_find_work_url(url: str) -> bool:
    return is_upwork_url(url) and urlparse(url).path.startswith("/nx/find-work")
