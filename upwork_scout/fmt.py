"""Human-readable formatting shared by scoring explanations and the report (Russian UI)."""

from __future__ import annotations

from datetime import datetime

from .models import Job

NOT_SPECIFIED = "не указано"
RECOMMENDATION_RU = {"apply": "Откликнуться", "review": "Проверить вручную", "skip": "Пропустить"}
LOCATION_RU = {"title": "в заголовке", "skills": "в навыках", "description": "в описании"}


def money(value: float | None) -> str:
    if value is None:
        return NOT_SPECIFIED
    if abs(value - round(value)) < 0.005:
        return f"${round(value):,}"
    return f"${value:,.2f}"


def budget(job: Job) -> str:
    if job.job_type == "fixed":
        return f"Fixed-price: {money(job.budget)}" if job.budget is not None else "Fixed-price, бюджет не указан"
    if job.job_type == "hourly":
        lo, hi = job.hourly_min, job.hourly_max
        if lo is None and hi is None:
            return "Hourly, ставка не указана"
        if lo is not None and hi is not None and lo != hi:
            return f"Hourly: {money(lo)}–{money(hi)}/ч"
        return f"Hourly: {money(hi if hi is not None else lo)}/ч"
    return NOT_SPECIFIED


def age(hours: float) -> str:
    if hours < 1:
        return f"~{max(1, round(hours * 60))} мин назад"
    if hours < 48:
        return f"~{round(hours)} ч назад"
    return f"~{round(hours / 24)} дн назад"


def age_hours(posted_at: str | None, now: datetime) -> float | None:
    if not posted_at:
        return None
    try:
        posted = datetime.fromisoformat(posted_at)
    except ValueError:
        return None
    if posted.tzinfo is None or now.tzinfo is None:
        posted, now = posted.replace(tzinfo=None), now.replace(tzinfo=None)
    return max(0.0, (now - posted).total_seconds() / 3600)
