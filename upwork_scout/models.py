"""Plain data objects shared by parsing, storage, scoring and reporting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Fields whose absence makes a deeper look at the job page worthwhile. The client rating is
# not listed: clients without reviews have no rating on the job page either.
KEY_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "job_type",
    "posted_text",
    "proposals_text",
    "payment_verified",
    "client_spend",
    "client_country",
)


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != []


@dataclass
class Job:
    """One Upwork job. ``None`` always means "not shown / not loaded", never a guessed value."""

    job_id: str
    url: str
    title: str | None = None
    description: str | None = None
    skills: list[str] = field(default_factory=list)
    posted_text: str | None = None
    posted_at: str | None = None  # ISO-8601 UTC estimate derived from posted_text
    job_type: str | None = None  # "fixed" | "hourly"
    budget: float | None = None  # fixed-price budget, USD
    hourly_min: float | None = None
    hourly_max: float | None = None
    budget_text: str | None = None
    experience_level: str | None = None
    duration: str | None = None
    proposals_text: str | None = None
    proposals_min: int | None = None
    proposals_max: int | None = None
    payment_verified: bool | None = None
    client_rating: float | None = None
    client_spend: float | None = None
    client_spend_text: str | None = None
    client_country: str | None = None

    def merged_with(self, newer: Job) -> Job:
        """Combine two observations of the same job: newer non-empty values win,
        the longer description wins (cards are often truncated), skills are united."""
        if newer.job_id != self.job_id:
            raise ValueError("cannot merge different jobs")
        data: dict[str, Any] = {}
        for f in fields(self):
            old, new = getattr(self, f.name), getattr(newer, f.name)
            if f.name == "skills":
                seen = {s.casefold() for s in old}
                data[f.name] = list(old) + [s for s in new if s.casefold() not in seen]
            elif f.name == "description":
                data[f.name] = new if len(new or "") > len(old or "") else old
            else:
                data[f.name] = new if _present(new) else old
        return Job(**data)

    def missing_fields(self) -> list[str]:
        missing = [name for name in KEY_FIELDS if not _present(getattr(self, name))]
        if self.job_type == "fixed" and self.budget is None:
            missing.append("budget")
        return missing

    def content_hash(self) -> str:
        """Hash of the fields an evaluation depends on (to reuse cached AI results)."""
        payload = {k: v for k, v in asdict(self).items() if k not in ("posted_text", "posted_at", "url")}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SavedSearch:
    search_id: str
    name: str
    url: str
