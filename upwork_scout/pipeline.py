"""Evaluation of one run's jobs (rules → optional AI) and assembly of the report context."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from .ai import AIEvaluator, AIOutcome, AIResult, ai_cache_key
from .config import Config
from .dedup import find_reposts
from .report import COMPONENTS, ReportContext, build_item, component_max
from .scoring import Evaluation, Scorer, apply_ai
from .storage import JobRecord, Storage

log = logging.getLogger(__name__)


@dataclass
class EvaluatedJob:
    record: JobRecord
    evaluation: Evaluation
    is_new: bool  # the ID was not in the database before this run
    in_scope: bool
    repost_of: str | None = None  # earlier job this one repeats under a new ID (dedup.py)

    @property
    def is_fresh(self) -> bool:
        """New to the user: a new ID that is not a repost of a job seen before."""
        return self.is_new and self.repost_of is None


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
    """Score every job seen in ``run_id``; ask Jev about the in-scope candidates.
    Reposts of earlier jobs are not fresh: with ``report.show: new`` they stay out of scope."""
    scorer = Scorer(cfg.filters, cfg.scoring)
    records = [rec for rec in map(store.get_record, store.run_job_ids(run_id)) if rec is not None]
    reposts = find_reposts(store, cfg.dedup, records)
    items: list[EvaluatedJob] = []
    for rec in records:
        it = EvaluatedJob(rec, scorer.evaluate(rec.job, now), rec.first_seen_run_id == run_id, in_scope=False,
                          repost_of=reposts.get(rec.job.job_id))
        it.in_scope = cfg.report.show == "all" or it.is_fresh
        items.append(it)

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


def translate_items(store: Storage, cfg: Config, items: list[EvaluatedJob], translator, now: datetime
                    ) -> dict[str, tuple[str, str]]:
    """Russian title + summary for the jobs the report lists prominently (translate.scope),
    best first; cached translations are reused without an API call."""
    from .translate import cache_key  # local import: keeps httpx-free imports for rescoring tests

    scope = [it for it in items if it.in_scope and not it.evaluation.excluded]
    if cfg.translate.scope == "passed":
        scope = [it for it in scope if it.evaluation.passed]
    scope.sort(key=_sort_key)
    out: dict[str, tuple[str, str]] = {}
    for it in scope:
        job = it.record.job
        key = cache_key(job, cfg.translate)
        cached = store.get_translation(job.job_id, key)
        if cached:
            out[job.job_id] = cached
            if translator is not None:
                translator.cached += 1
            continue
        if translator is None:
            continue
        result = translator.translate(job)
        if result is not None:
            store.save_translation(job.job_id, key, result.title_ru, result.summary_ru, cfg.translate.model, now)
            out[job.job_id] = (result.title_ru, result.summary_ru)
    return out


def build_context(store: Storage, cfg: Config, run_id: int, items: list[EvaluatedJob], now_utc: datetime,
                  now_local: datetime, abort_reason: str | None, warnings: list[str],
                  ai_summary: str, details_summary: str | None,
                  translations: dict[str, tuple[str, str]] | None = None) -> ReportContext:
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
        new_jobs=sum(1 for it in items if it.is_fresh),
        reposted_jobs=sum(1 for it in items if it.is_new and it.repost_of),
        abort_reason=abort_reason,
        warnings=list(warnings),
        ai_summary=ai_summary,
        details_summary=details_summary,
    )
    maxima = component_max(cfg.scoring)
    ctx.components = [(key, label, maxima[key]) for key, label in COMPONENTS]

    def item(it: EvaluatedJob) -> dict:
        names = [name for _, name in store.searches_for_job(it.record.job.job_id)]
        original = store.get_job(it.repost_of) if it.repost_of else None
        return build_item(it.record.job, it.evaluation, it.is_fresh, names, now_utc, cfg.scoring,
                          (translations or {}).get(it.record.job.job_id), original)

    scope = sorted((it for it in items if it.in_scope), key=_sort_key)
    passed = [it for it in scope if it.evaluation.passed]
    ctx.passed_in_scope = len(passed)
    ctx.main = [item(it) for it in passed]
    if cfg.report.show_below_threshold:
        ctx.below = [item(it) for it in scope if not it.evaluation.passed and not it.evaluation.excluded]
    if cfg.report.show_excluded:
        ctx.excluded = [item(it) for it in scope if it.evaluation.excluded]
    ctx.reposts = [item(it) for it in sorted(items, key=_sort_key) if it.is_new and it.repost_of and not it.in_scope]
    return ctx
