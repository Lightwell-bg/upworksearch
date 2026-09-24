"""Evaluation of one run's jobs (rules → optional AI) and assembly of the report context."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .ai import AIEvaluator, AIOutcome, AIResult, ai_cache_key
from .config import Config
from .report import COMPONENTS, ReportContext, build_item, component_max
from .scoring import Evaluation, Scorer, apply_ai
from .storage import JobRecord, Storage

log = logging.getLogger(__name__)


@dataclass
class EvaluatedJob:
    record: JobRecord
    evaluation: Evaluation
    is_new: bool
    in_scope: bool


def _cached_outcome(row) -> AIOutcome:
    result = AIResult(
        relevance=int(row["ai_relevance"]),
        relevance_confidence=None,
        recommendation=row["ai_recommendation"],
        recommendation_p=row["ai_recommendation_p"],
        risks=[],
        explanation=(row["ai_explanation"] or "") + " (результат из кеша: вакансия не изменилась)",
        model=row["ai_model"],
        stored_risk_labels=json.loads(row["ai_risks"] or "[]"),
    )
    return AIOutcome("cached", None, "cached", result)


def evaluate_run(store: Storage, cfg: Config, run_id: int, now: datetime,
                 evaluator: AIEvaluator | None) -> list[EvaluatedJob]:
    """Score every job seen in ``run_id``; ask Jev about the in-scope candidates."""
    scorer = Scorer(cfg.filters, cfg.scoring)
    items: list[EvaluatedJob] = []
    for job_id in store.run_job_ids(run_id):
        rec = store.get_record(job_id)
        if rec is None:
            continue
        is_new = rec.first_seen_run_id == run_id
        items.append(EvaluatedJob(rec, scorer.evaluate(rec.job, now), is_new,
                                  in_scope=cfg.report.show == "all" or is_new))

    use_ai = cfg.ai.enabled and evaluator is not None
    if use_ai:
        candidates = [
            it for it in items
            if it.in_scope
            and (not it.evaluation.excluded or cfg.ai.override_exclusions)
            and it.evaluation.rule_score >= cfg.ai.min_rule_score
        ]
        candidates.sort(key=lambda it: -it.evaluation.rule_score)
        model = evaluator.model if evaluator is not None else None
        for it in candidates:
            key = ai_cache_key(it.record.job, cfg.ai, model)
            it.evaluation.ai_cache_key = key
            cached = store.cached_ai(it.record.job.job_id, key)
            if cached is not None and cached["ai_relevance"] is not None and cached["ai_recommendation"]:
                outcome = _cached_outcome(cached)
            elif evaluator is not None and evaluator.disabled_reason:
                continue  # stated once in the report summary, not on every job
            else:
                try:
                    outcome = evaluator.evaluate(it.record.job)
                except Exception as exc:  # a bug in the AI stage must not lose the report
                    log.exception("AI-этап: внутренняя ошибка")
                    outcome = AIOutcome("failed", f"AI-оценка не выполнена: внутренняя ошибка {type(exc).__name__}",
                                        "failed_internal")
            apply_ai(it.evaluation, outcome, cfg.ai, scorer)

    for it in items:
        store.save_evaluation(run_id, it.evaluation, it.record.job.content_hash(), now)
    return items


def _sort_key(it: EvaluatedJob) -> tuple:
    return (-it.evaluation.final_score, -it.evaluation.rule_score, it.record.job.posted_at or "")


def build_context(store: Storage, cfg: Config, run_id: int, items: list[EvaluatedJob], now_utc: datetime,
                  now_local: datetime, abort_reason: str | None, warnings: list[str],
                  ai_summary: str, details_summary: str | None) -> ReportContext:
    searches = store.search_runs(run_id)
    ctx = ReportContext(
        run_id=run_id,
        generated_at=now_local,
        show_mode=cfg.report.show,
        min_score=cfg.scoring.min_score,
        searches=searches,
        searches_found=len(searches),
        searches_ok=sum(1 for s in searches if s.status == "ok"),
        search_errors=sum(1 for s in searches if s.status == "error"),
        unique_jobs=len(items),
        new_jobs=sum(1 for it in items if it.is_new),
        abort_reason=abort_reason,
        warnings=list(warnings),
        ai_summary=ai_summary,
        details_summary=details_summary,
    )
    maxima = component_max(cfg.scoring)
    ctx.components = [(key, label, maxima[key]) for key, label in COMPONENTS]

    def item(it: EvaluatedJob) -> dict:
        names = [name for _, name in store.searches_for_job(it.record.job.job_id)]
        return build_item(it.record.job, it.evaluation, it.is_new, names, now_utc, cfg.scoring)

    scope = sorted((it for it in items if it.in_scope), key=_sort_key)
    passed = [it for it in scope if it.evaluation.passed]
    ctx.passed_in_scope = len(passed)
    ctx.main = [item(it) for it in passed]
    if cfg.report.show_below_threshold:
        ctx.below = [item(it) for it in scope if not it.evaluation.passed and not it.evaluation.excluded]
    if cfg.report.show_excluded:
        ctx.excluded = [item(it) for it in scope if it.evaluation.excluded]
    return ctx
