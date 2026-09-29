"""Tests for upwork_scout.dedup: reposted jobs under a new ID, and their effect on the pipeline."""

from __future__ import annotations

from datetime import timedelta

from upwork_scout.config import DedupCfg
from upwork_scout.dedup import find_reposts
from upwork_scout.models import Job, SavedSearch
from upwork_scout.pipeline import build_context, evaluate_run
from upwork_scout.storage import Storage

from .conftest import NOW

# Report links are rebuilt from validated IDs, so pipeline tests need real-looking ones.
ORIG, AGAIN, WP = "0100000000000000a1", "0100000000000000a2", "0100000000000000a3"

SEARCH = SavedSearch(search_id="1", name="S", url="https://www.upwork.com/nx/find-work/1")

DESC = ("We need an experienced Python developer to build a FastAPI service that pulls orders from "
        "the Shopify API, stores them in PostgreSQL and sends a daily summary to a Telegram channel. "
        "Deliverables: source code, Docker setup, short README. Budget is fixed, timeline two weeks.")
OTHER = ("Looking for a WordPress expert to fix a broken WooCommerce checkout page, update plugins and "
         "speed up the site. Must have experience with caching plugins and page speed optimization, "
         "please share examples of similar work in your proposal.")


def job(job_id: str, title: str = "Python FastAPI developer for Shopify integration", description: str = DESC,
        country: str | None = "United States") -> Job:
    return Job(job_id=job_id, url=f"https://www.upwork.com/jobs/~{job_id}", title=title,
               description=description, client_country=country, job_type="fixed", budget=800.0,
               skills=["python", "fastapi"])


def record(store: Storage, jobs: list[Job], at=NOW) -> int:
    run_id = store.start_run(at)
    store.upsert_searches([SEARCH], at)
    store.record_jobs(run_id, SEARCH, jobs, at)
    return run_id


def reposts_of_run(store: Storage, run_id: int, cfg: DedupCfg | None = None) -> dict[str, str]:
    records = [store.get_record(j) for j in store.run_job_ids(run_id)]
    return find_reposts(store, cfg or DedupCfg(), records)


class TestFindReposts:
    def test_identical_text_under_new_id_is_a_repost(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=2))
        run_id = record(store, [job("again")])
        assert reposts_of_run(store, run_id) == {"again": "orig"}

    def test_truncated_card_matches_full_description(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=1))
        truncated = DESC[:180].rsplit(" ", 1)[0] + "…"
        run_id = record(store, [job("again", description=truncated)])
        assert reposts_of_run(store, run_id) == {"again": "orig"}

    def test_small_edits_still_match(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=1))
        edited = DESC.replace("two weeks", "ten days") + " Thanks!"
        run_id = record(store, [job("again", title="Python / FastAPI developer for Shopify integration",
                                    description=edited)])
        assert reposts_of_run(store, run_id) == {"again": "orig"}

    def test_different_job_is_not_a_repost(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=1))
        run_id = record(store, [job("wp", title="WordPress WooCommerce fix", description=OTHER)])
        assert reposts_of_run(store, run_id) == {}

    def test_same_text_different_title_is_not_a_repost(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=1))
        run_id = record(store, [job("again", title="Senior data engineer wanted")])
        assert reposts_of_run(store, run_id) == {}

    def test_different_client_country_is_not_a_repost(self, store: Storage):
        record(store, [job("orig", country="Germany")], NOW - timedelta(days=1))
        run_id = record(store, [job("again", country="United States")])
        assert reposts_of_run(store, run_id) == {}

    def test_missing_country_does_not_block(self, store: Storage):
        record(store, [job("orig", country=None)], NOW - timedelta(days=1))
        run_id = record(store, [job("again")])
        assert reposts_of_run(store, run_id) == {"again": "orig"}

    def test_outside_window_is_not_a_repost(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=40))
        run_id = record(store, [job("again")])
        assert reposts_of_run(store, run_id, DedupCfg(window_days=30)) == {}

    def test_short_texts_need_an_exact_match(self, store: Storage):
        record(store, [job("orig", description="Need a quick Python script fix today please")],
               NOW - timedelta(days=1))
        run_id = record(store, [job("a", description="Need a quick Python script fix today please"),
                                job("b", description="Need a quick Python script fix tomorrow please")])
        assert reposts_of_run(store, run_id) == {"a": "orig"}

    def test_duplicates_within_one_run_keep_the_first(self, store: Storage):
        run_id = record(store, [job("first"), job("second")])
        assert reposts_of_run(store, run_id) == {"second": "first"}

    def test_points_to_earliest_original(self, store: Storage):
        record(store, [job("a")], NOW - timedelta(days=5))
        record(store, [job("b")], NOW - timedelta(days=2))
        run_id = record(store, [job("c")])
        assert reposts_of_run(store, run_id) == {"c": "a"}

    def test_later_job_is_never_the_original_of_an_earlier_one(self, store: Storage):
        early = record(store, [job("orig")], NOW - timedelta(days=1))
        record(store, [job("again")])
        assert reposts_of_run(store, early) == {}  # a rescore of the old run gives the same answer

    def test_disabled(self, store: Storage):
        record(store, [job("orig")], NOW - timedelta(days=1))
        run_id = record(store, [job("again")])
        assert reposts_of_run(store, run_id, DedupCfg(enabled=False)) == {}


class TestPipeline:
    def test_repost_is_hidden_in_show_new_and_skipped_by_ai(self, store: Storage, cfg):
        cfg.ai.enabled = False
        record(store, [job(ORIG)], NOW - timedelta(days=1))
        run_id = record(store, [job(AGAIN), job(WP, title="WordPress WooCommerce fix", description=OTHER)])
        items = evaluate_run(store, cfg, run_id, NOW, None)
        by_id = {it.record.job.job_id: it for it in items}

        assert by_id[AGAIN].repost_of == ORIG
        assert by_id[AGAIN].is_new and not by_id[AGAIN].is_fresh
        assert by_id[AGAIN].in_scope is False
        assert by_id[WP].in_scope is True

        ctx = build_context(store, cfg, run_id, items, NOW, NOW, None, [], "", None)
        assert ctx.new_jobs == 1
        assert ctx.reposted_jobs == 1
        assert [it["job_id"] for it in ctx.reposts] == [AGAIN]
        assert ctx.reposts[0]["repost_of"]["url"].endswith("~" + ORIG)
        shown = [it["job_id"] for it in ctx.main + ctx.below + ctx.excluded]
        assert AGAIN not in shown

    def test_repost_is_marked_in_show_all(self, store: Storage, cfg):
        cfg.ai.enabled = False
        cfg.report.show = "all"
        record(store, [job(ORIG)], NOW - timedelta(days=1))
        run_id = record(store, [job(AGAIN)])
        items = evaluate_run(store, cfg, run_id, NOW, None)
        ctx = build_context(store, cfg, run_id, items, NOW, NOW, None, [], "", None)

        entry = next(it for it in ctx.main + ctx.below + ctx.excluded if it["job_id"] == AGAIN)
        assert entry["is_new"] is False
        assert entry["repost_of"]["title"] == "Python FastAPI developer for Shopify integration"
        assert ctx.reposts == []
