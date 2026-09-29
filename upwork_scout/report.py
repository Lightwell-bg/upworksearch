"""HTML report rendering. All Upwork data pass through Jinja2 autoescaping; links are
rebuilt from validated IDs, so nothing from the page can inject markup or a javascript: URL."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import __version__, localize
from .config import ScoringCfg
from .fmt import NOT_SPECIFIED, RECOMMENDATION_RU, age, age_hours
from .ids import job_url
from .models import Job
from .scoring import Evaluation

TEMPLATES_DIR = Path(__file__).parent / "templates"
COMPONENTS = [
    ("stack", "стек"),
    ("freshness", "свежесть"),
    ("competition", "отклики"),
    ("client", "клиент"),
    ("budget", "бюджет"),
    ("clarity", "ясность"),
]
STATUS_RU = {"ok": "обработан", "error": "ошибка", "aborted": "прерван", "skipped": "не обработан"}


def component_max(s: ScoringCfg) -> dict[str, float]:
    return {
        "stack": s.stack.max_points,
        "freshness": s.freshness.max_points,
        "competition": s.competition.max_points,
        "client": s.client.payment_verified_points + s.client.spend_points + s.client.rating_points,
        "budget": s.budget.max_points,
        "clarity": s.clarity.max_points,
    }


def _client_text(job: Job) -> str:
    parts: list[str] = []
    if job.payment_verified is not None:
        parts.append("оплата подтверждена" if job.payment_verified else "оплата не подтверждена")
    if job.client_rating is not None:
        parts.append(f"рейтинг {job.client_rating:g}")
    if job.client_spend_text or job.client_spend is not None:
        parts.append(f"потратил {job.client_spend_text or job.client_spend}")
    if job.client_country:
        parts.append(localize.country(job.client_country))
    return ", ".join(parts) if parts else NOT_SPECIFIED


def build_item(job: Job, ev: Evaluation, is_new: bool, searches: list[str], now: datetime,
               scoring: ScoringCfg, translation: tuple[str, str] | None = None,
               repost_of: Job | None = None) -> dict[str, Any]:
    """Report entry in Russian: Upwork labels via :mod:`localize`, title/summary via
    ``translation`` = (title_ru, summary_ru) when available (the original stays visible).
    ``repost_of`` is the earlier job this one repeats (see dedup.py)."""
    maxima = component_max(scoring)
    hours = age_hours(job.posted_at, now)
    posted = localize.posted(job.posted_text) or NOT_SPECIFIED
    if job.posted_text and hours is not None and "ago" not in job.posted_text.lower():
        posted = f"{posted} ({age(hours)})"
    level = "; ".join(x for x in (localize.experience(job.experience_level), localize.duration(job.duration)) if x)
    original_title = job.title or f"Вакансия {job.job_id}"
    title_ru, summary_ru = translation if translation else (None, None)
    return {
        "job_id": job.job_id,
        "url": job_url(job.job_id),
        "title": title_ru or original_title,
        "title_original": original_title if title_ru else None,
        "summary_ru": summary_ru,
        "description": job.description,
        "skills": job.skills,
        "budget": localize.budget(job),
        "proposals": localize.proposals(job.proposals_text) or NOT_SPECIFIED,
        "posted": posted,
        "level_duration": level or NOT_SPECIFIED,
        "client": _client_text(job),
        "is_new": is_new,
        "repost_of": {"url": job_url(repost_of.job_id), "title": repost_of.title or f"Вакансия {repost_of.job_id}"}
                     if repost_of else None,
        "searches": searches or [NOT_SPECIFIED],
        "final_score": ev.final_score,
        "rule_score": ev.rule_score,
        "ai_relevance": ev.ai_relevance,
        "recommendation": ev.recommendation,
        "recommendation_ru": RECOMMENDATION_RU[ev.recommendation],
        "reasons": ev.reasons,
        "risks": ev.risks + [f"AI: {r}" for r in ev.ai_risks],
        "ai_explanation": ev.ai_explanation,
        "ai_message": ev.ai_message,
        "exclusion_reasons": ev.exclusion_reasons,
        "bar": [(key, label, maxima[key], min(ev.components.get(key, 0.0), maxima[key])) for key, label in COMPONENTS],
        "sort": sort_keys(job, ev, hours),
    }


def sort_keys(job: Job, ev: Evaluation, hours: float | None) -> dict[str, float | None]:
    """Numeric values for client-side sorting in the report (``None`` = unknown, sorted last)."""
    proposals = job.proposals_max if job.proposals_max is not None else job.proposals_min
    return {
        "score": ev.final_score,
        "rule": ev.rule_score,
        "ai": ev.ai_relevance,
        "age": round(hours, 2) if hours is not None else None,
        "fixed": job.budget if job.job_type == "fixed" else None,
        "hourly": (job.hourly_max if job.hourly_max is not None else job.hourly_min) if job.job_type == "hourly" else None,
        "proposals": proposals,
        "spend": job.client_spend,
        "rating": job.client_rating,
    }


def search_index(groups: list[list[dict[str, Any]]]) -> dict[str, int]:
    """Search names that appear in the given report groups -> stable filter index (by name)."""
    names = sorted({n for items in groups for it in items for n in it["searches"]}, key=str.casefold)
    return {name: i for i, name in enumerate(names)}


@dataclass
class ReportContext:
    run_id: int
    generated_at: datetime  # local time
    show_mode: str
    min_score: float
    searches: list[Any] = field(default_factory=list)  # storage.SearchRunRow (+ url)
    searches_found: int = 0
    searches_ok: int = 0
    search_errors: int = 0
    unique_jobs: int = 0
    new_jobs: int = 0
    reposted_jobs: int = 0
    passed_in_scope: int = 0
    main: list[dict[str, Any]] = field(default_factory=list)
    below: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
    reposts: list[dict[str, Any]] = field(default_factory=list)  # new IDs hidden as reposts
    abort_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    ai_summary: str = ""
    details_summary: str | None = None
    components: list[tuple[str, str, float]] = field(default_factory=list)


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"], default_for_string=True, default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_report(ctx: ReportContext) -> str:
    template = _environment().get_template("report.html.j2")
    return template.render(
        **{k: getattr(ctx, k) for k in ctx.__dataclass_fields__},
        search_index=search_index([ctx.main, ctx.below]),
        generated_at_str=ctx.generated_at.strftime("%d.%m.%Y %H:%M"),
        status_ru=STATUS_RU,
        version=__version__,
    )


def report_path(output_dir: Path, now_local: datetime) -> Path:
    """outputs/upwork-report-YYYY-MM-DD-HH-mm.html; a suffix is added if the name is taken."""
    output_dir.mkdir(parents=True, exist_ok=True)
    base = f"upwork-report-{now_local:%Y-%m-%d-%H-%M}"
    path = output_dir / f"{base}.html"
    n = 2
    while path.exists():
        path = output_dir / f"{base}-{n}.html"
        n += 1
    return path


def write_report(ctx: ReportContext, output_dir: Path) -> Path:
    path = report_path(output_dir, ctx.generated_at)
    path.write_text(render_report(ctx), encoding="utf-8")
    return path
