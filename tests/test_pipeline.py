"""Tests for upwork_scout.pipeline: scoring a run, AI-caching and scope control."""

from __future__ import annotations

from upwork_scout.ai import AIOutcome, AIResult
from upwork_scout.models import Job, SavedSearch
from upwork_scout.pipeline import evaluate_run
from upwork_scout.storage import Storage

from .conftest import NOW


class FakeEvaluator:
    """Minimal stand-in for AIEvaluator: no HTTP, just counts calls."""

    def __init__(self):
        self.disabled_reason = None
        self.model = "fake-model"  # real AIEvaluator always exposes .model, used to build the cache key
        self.calls = 0
        self.seen_ids: list[str] = []

    def evaluate(self, job: Job) -> AIOutcome:
        self.calls += 1
        self.seen_ids.append(job.job_id)
        result = AIResult(relevance=90, relevance_confidence=0.9, recommendation="apply",
                          recommendation_p=0.8, risks=[], explanation="ok", model="fake")
        return AIOutcome("ok", None, "ok", result)


def seed(store: Storage) -> tuple[int, Job, Job]:
    run_id = store.start_run(NOW)
    search = SavedSearch(search_id="1", name="S", url="https://www.upwork.com/nx/find-work/1")
    store.upsert_searches([search], NOW)
    good = Job(job_id="good", url="https://www.upwork.com/jobs/~good", title="Python FastAPI dev",
              description="x" * 400, job_type="fixed", budget=1000.0, skills=["python", "fastapi"])
    excluded = Job(job_id="bad", url="https://www.upwork.com/jobs/~bad", title="SEO expert",
                  job_type="fixed", budget=50.0)
    store.record_jobs(run_id, search, [good, excluded], NOW)
    return run_id, good, excluded


class TestEvaluateRun:
    def test_calls_evaluator_for_in_scope_candidates(self, store: Storage, cfg):
        run_id, good, excluded = seed(store)
        evaluator = FakeEvaluator()
        items = evaluate_run(store, cfg, run_id, NOW, evaluator)

        assert evaluator.calls == 1
        assert evaluator.seen_ids == ["good"]
        by_id = {it.record.job.job_id: it for it in items}
        assert by_id["good"].evaluation.ai_status == "ok"
        assert by_id["bad"].evaluation.excluded is True
        assert by_id["bad"].evaluation.ai_status is None

    def test_excluded_jobs_never_sent_when_override_exclusions_false(self, store: Storage, cfg):
        cfg.ai.override_exclusions = False
        run_id, good, excluded = seed(store)
        evaluator = FakeEvaluator()
        evaluate_run(store, cfg, run_id, NOW, evaluator)
        assert "bad" not in evaluator.seen_ids

    def test_rescore_reuses_cached_results(self, store: Storage, cfg):
        run_id, good, excluded = seed(store)
        evaluator = FakeEvaluator()
        evaluate_run(store, cfg, run_id, NOW, evaluator)
        assert evaluator.calls == 1

        items2 = evaluate_run(store, cfg, run_id, NOW, evaluator)
        assert evaluator.calls == 1  # no new HTTP-equivalent call: served from the cache

        by_id = {it.record.job.job_id: it for it in items2}
        assert by_id["good"].evaluation.ai_status == "cached"

    def test_changing_freelancer_focus_invalidates_the_cache(self, store: Storage, cfg):
        run_id, good, excluded = seed(store)
        evaluator = FakeEvaluator()
        evaluate_run(store, cfg, run_id, NOW, evaluator)
        assert evaluator.calls == 1

        # The questions sent to Jev depend on ai.freelancer_focus, so a config change must be
        # a cache miss (the old cached answer no longer reflects what would be asked now).
        cfg.ai.freelancer_focus = "Completely different focus, only Rust and Go"
        evaluate_run(store, cfg, run_id, NOW, evaluator)
        assert evaluator.calls == 2
