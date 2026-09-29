"""Tests for upwork_scout.pipeline: scoring a run, AI-caching and scope control."""

from __future__ import annotations

from upwork_scout.ai import AIOutcome, AIResult
from upwork_scout.models import Job, SavedSearch
from upwork_scout.pipeline import evaluate_run, translate_items
from upwork_scout.storage import Storage
from upwork_scout.translate import Translation

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


class FakeTranslator:
    """Minimal stand-in for Translator: no HTTP, just counts calls (cached is bumped by
    pipeline.translate_items itself, like it does for the real Translator)."""

    def __init__(self):
        self.cached = 0
        self.calls = 0
        self.seen_ids: list[str] = []

    def translate(self, job: Job) -> Translation:
        self.calls += 1
        self.seen_ids.append(job.job_id)
        return Translation(title_ru=f"РУ {job.title}", summary_ru="Краткое описание на русском.")


# Canonical (valid) ids: build_item/job_url validate the id shape, unlike evaluate_run.
PASSED_ID = "01000000000000000001"
EXCLUDED_ID = "01000000000000000002"
BELOW_ID = "01000000000000000003"


def seed_for_translation(store: Storage, cfg) -> int:
    run_id = store.start_run(NOW)
    search = SavedSearch(search_id="1", name="S", url="https://www.upwork.com/nx/find-work/1")
    store.upsert_searches([search], NOW)
    passed = Job(job_id=PASSED_ID, url=f"https://www.upwork.com/jobs/~{PASSED_ID}", title="Python FastAPI dev",
                description="x" * 400, job_type="fixed", budget=1000.0, skills=["python", "fastapi"])
    excluded = Job(job_id=EXCLUDED_ID, url=f"https://www.upwork.com/jobs/~{EXCLUDED_ID}", title="SEO expert",
                  job_type="fixed", budget=50.0)
    below = Job(job_id=BELOW_ID, url=f"https://www.upwork.com/jobs/~{BELOW_ID}", title="Generic job")
    store.record_jobs(run_id, search, [passed, excluded, below], NOW)
    return run_id


class TestTranslateItems:
    def test_scope_passed_only_translates_passed_non_excluded_jobs(self, store: Storage, cfg):
        run_id = seed_for_translation(store, cfg)
        items = evaluate_run(store, cfg, run_id, NOW, None)
        by_id = {it.record.job.job_id: it for it in items}
        assert by_id[EXCLUDED_ID].evaluation.excluded is True
        assert by_id[PASSED_ID].evaluation.passed is True
        assert by_id[BELOW_ID].evaluation.passed is False and by_id[BELOW_ID].evaluation.excluded is False

        cfg.translate.scope = "passed"
        translator = FakeTranslator()
        result = translate_items(store, cfg, items, translator, NOW)

        assert set(result.keys()) == {PASSED_ID}
        assert translator.seen_ids == [PASSED_ID]

    def test_scope_shown_translates_all_non_excluded_shown_jobs(self, store: Storage, cfg):
        run_id = seed_for_translation(store, cfg)
        items = evaluate_run(store, cfg, run_id, NOW, None)

        cfg.translate.scope = "shown"
        translator = FakeTranslator()
        result = translate_items(store, cfg, items, translator, NOW)

        assert set(result.keys()) == {PASSED_ID, BELOW_ID}
        assert EXCLUDED_ID not in result

    def test_cached_translation_is_reused_without_calling_translator(self, store: Storage, cfg):
        run_id = seed_for_translation(store, cfg)
        items = evaluate_run(store, cfg, run_id, NOW, None)
        cfg.translate.scope = "passed"

        first = FakeTranslator()
        translate_items(store, cfg, items, first, NOW)
        assert first.calls == 1

        second = FakeTranslator()
        result2 = translate_items(store, cfg, items, second, NOW)
        assert second.calls == 0
        assert second.cached == 1
        assert set(result2.keys()) == {PASSED_ID}

    def test_translator_none_returns_only_cached_translations(self, store: Storage, cfg):
        run_id = seed_for_translation(store, cfg)
        items = evaluate_run(store, cfg, run_id, NOW, None)
        cfg.translate.scope = "passed"

        translate_items(store, cfg, items, FakeTranslator(), NOW)  # populates the cache

        result = translate_items(store, cfg, items, None, NOW)
        assert set(result.keys()) == {PASSED_ID}


class TestBuildItemTranslation:
    def test_with_translation_title_is_russian_and_original_is_english(self, store: Storage, cfg):
        from upwork_scout.report import build_item

        run_id = seed_for_translation(store, cfg)
        items = evaluate_run(store, cfg, run_id, NOW, None)
        it = next(i for i in items if i.record.job.job_id == PASSED_ID)
        item = build_item(it.record.job, it.evaluation, it.is_new, ["S"], NOW, cfg.scoring,
                          ("РУ Python FastAPI dev", "Краткое описание на русском."))
        assert item["title"] == "РУ Python FastAPI dev"
        assert item["title_original"] == "Python FastAPI dev"
        assert item["summary_ru"] == "Краткое описание на русском."

    def test_without_translation_title_original_is_none(self, store: Storage, cfg):
        from upwork_scout.report import build_item

        run_id = seed_for_translation(store, cfg)
        items = evaluate_run(store, cfg, run_id, NOW, None)
        it = next(i for i in items if i.record.job.job_id == PASSED_ID)
        item = build_item(it.record.job, it.evaluation, it.is_new, ["S"], NOW, cfg.scoring, None)
        assert item["title"] == "Python FastAPI dev"
        assert item["title_original"] is None
        assert item["summary_ru"] is None
