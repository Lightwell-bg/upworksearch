"""HTML report rendering. All Upwork data pass through Jinja2 autoescaping; links are
rebuilt from validated IDs, so nothing from the page can inject markup or a javascript: URL."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from . import __version__
from .config import ScoringCfg
from .fmt import NOT_SPECIFIED, RECOMMENDATION_RU, age, age_hours, budget
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
        parts.append(job.client_country)
    return ", ".join(parts) if parts else NOT_SPECIFIED


def build_item(job: Job, ev: Evaluation, is_new: bool, searches: list[str], now: datetime,
               scoring: ScoringCfg) -> dict[str, Any]:
    maxima = component_max(scoring)
    hours = age_hours(job.posted_at, now)
    posted = job.posted_text or NOT_SPECIFIED
    if job.posted_text and hours is not None and "ago" not in job.posted_text.lower():
        posted = f"{job.posted_text} ({age(hours)})"
    level = "; ".join(x for x in (job.experience_level, job.duration) if x) or NOT_SPECIFIED
    return {
        "job_id": job.job_id,
        "url": job_url(job.job_id),
        "title": job.title or f"Вакансия {job.job_id}",
        "description": job.description,
        "skills": job.skills,
        "budget": budget(job),
        "proposals": job.proposals_text or NOT_SPECIFIED,
        "posted": posted,
        "level_duration": level,
        "client": _client_text(job),
        "is_new": is_new,
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
    }


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
    passed_in_scope: int = 0
    main: list[dict[str, Any]] = field(default_factory=list)
    below: list[dict[str, Any]] = field(default_factory=list)
    excluded: list[dict[str, Any]] = field(default_factory=list)
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
