"""Tests for upwork_scout.storage: SQLite persistence, dedup/merge and evaluation cache."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from upwork_scout.models import Job, SavedSearch
from upwork_scout.scoring import Evaluation
from upwork_scout.storage import Storage

from .conftest import NOW


def make_search(sid="1", name="Search"):
    return SavedSearch(search_id=sid, name=name, url=f"https://www.upwork.com/nx/find-work/{sid}")


class TestDedupAcrossSearches:
    def test_same_job_from_two_searches_produces_one_jobs_row(self, store: Storage):
        run_id = store.start_run(NOW)
        search_a, search_b = make_search("1", "Search A"), make_search("2", "Search B")
        store.upsert_searches([search_a, search_b], NOW)

        job_a = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job",
                    description="short", skills=["Python"], payment_verified=False)
        job_b = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job",
                    description="a much longer description here", skills=["FastAPI"], payment_verified=False)
        store.record_jobs(run_id, search_a, [job_a], NOW)
        store.record_jobs(run_id, search_b, [job_b], NOW)

        assert store.count_jobs() == 1

    def test_two_job_searches_rows(self, store: Storage):
        run_id = store.start_run(NOW)
        search_a, search_b = make_search("1", "Search A"), make_search("2", "Search B")
        store.upsert_searches([search_a, search_b], NOW)
        job = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")
        store.record_jobs(run_id, search_a, [job], NOW)
        store.record_jobs(run_id, search_b, [job], NOW)

        count = store.conn.execute("SELECT COUNT(*) FROM job_searches WHERE job_id='abc'").fetchone()[0]
        assert count == 2

    def test_searches_for_job_returns_both_names(self, store: Storage):
        run_id = store.start_run(NOW)
        search_a, search_b = make_search("1", "Search A"), make_search("2", "Search B")
        store.upsert_searches([search_a, search_b], NOW)
        job = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")
        store.record_jobs(run_id, search_a, [job], NOW)
        store.record_jobs(run_id, search_b, [job], NOW)

        names = {name for _, name in store.searches_for_job("abc")}
        assert names == {"Search A", "Search B"}

    def test_merge_keeps_longer_description_and_unions_skills(self, store: Storage):
        run_id = store.start_run(NOW)
        search_a, search_b = make_search("1", "Search A"), make_search("2", "Search B")
        store.upsert_searches([search_a, search_b], NOW)
        job_a = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job",
                    description="short", skills=["Python"])
        job_b = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job",
                    description="a much longer description here", skills=["FastAPI"])
        store.record_jobs(run_id, search_a, [job_a], NOW)
        store.record_jobs(run_id, search_b, [job_b], NOW)

        merged = store.get_job("abc")
        assert merged.description == "a much longer description here"
        assert merged.skills == ["Python", "FastAPI"]

    def test_payment_verified_false_round_trips_as_false_not_none(self, store: Storage):
        run_id = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        job = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job", payment_verified=False)
        store.record_jobs(run_id, search, [job], NOW)

        loaded = store.get_job("abc")
        assert loaded.payment_verified is False


class TestRerun:
    def test_job_from_run1_is_not_new_in_run2(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        job = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")
        store.record_jobs(run1, search, [job], NOW)

        run2 = store.start_run(NOW)
        store.record_jobs(run2, search, [job], NOW)

        rec = store.get_record("abc")
        assert rec.first_seen_run_id == run1
        assert rec.first_seen_run_id != run2

    def test_job_first_seen_in_run2_is_new(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        store.record_jobs(run1, search, [Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")], NOW)

        run2 = store.start_run(NOW)
        new_job = Job(job_id="xyz", url="https://www.upwork.com/jobs/~xyz", title="New Job")
        store.record_jobs(run2, search, [new_job], NOW)

        rec = store.get_record("xyz")
        assert rec.first_seen_run_id == run2

    def test_run_job_ids(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        store.record_jobs(run1, search, [Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")], NOW)

        run2 = store.start_run(NOW)
        store.record_jobs(run2, search, [Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")], NOW)
        store.record_jobs(run2, search, [Job(job_id="xyz", url="https://www.upwork.com/jobs/~xyz", title="X")], NOW)

        assert store.run_job_ids(run1) == ["abc"]
        assert set(store.run_job_ids(run2)) == {"abc", "xyz"}


class TestUpdateJobDetails:
    def test_sets_details_fetched_at_and_merges(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        store.record_jobs(
            run1, search,
            [Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job", description="short")],
            NOW,
        )
        assert store.get_record("abc").details_fetched_at is None

        details = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job",
                      description="full details page description text", job_type="fixed", budget=500.0)
        store.update_job_details(details, run1, NOW)

        rec = store.get_record("abc")
        assert rec.details_fetched_at is not None
        assert rec.job.budget == 500.0
        assert rec.job.description == "full details page description text"


class TestEvaluationsCache:
    """The AI cache is keyed by Evaluation.ai_cache_key (a fingerprint of the AI inputs — see
    ai.ai_cache_key), not by Job.content_hash(): content_hash also covers fields the AI is
    never even told about (e.g. posted_text), so it would invalidate the cache too eagerly."""

    def test_save_and_read_back(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        job = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")
        store.record_jobs(run1, search, [job], NOW)

        ev = Evaluation(
            job_id="abc", excluded=False, exclusion_reasons=[], rule_score=50, final_score=60,
            passed=True, recommendation="review", reasons=["r"], risks=[], components={"stack": 10},
            ai_status="ok", ai_code="ok", ai_message=None, ai_relevance=80, ai_recommendation="apply",
            ai_recommendation_p=0.7, ai_risks=["risk1"], ai_explanation="expl", ai_model="m",
            ai_cache_key="k1",
        )
        store.save_evaluation(run1, ev, job.content_hash(), NOW)

        cached = store.cached_ai("abc", "k1")
        assert cached is not None
        assert cached["ai_relevance"] == 80
        assert cached["ai_recommendation"] == "apply"

    def test_wrong_cache_key_returns_none(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        job = Job(job_id="abc", url="https://www.upwork.com/jobs/~abc", title="Job")
        store.record_jobs(run1, search, [job], NOW)
        ev = Evaluation(
            job_id="abc", excluded=False, exclusion_reasons=[], rule_score=50, final_score=60,
            passed=True, recommendation="review", reasons=[], risks=[], components={},
            ai_status="ok", ai_code="ok", ai_relevance=80, ai_recommendation="apply",
            ai_cache_key="k1",
        )
        store.save_evaluation(run1, ev, job.content_hash(), NOW)

        assert store.cached_ai("abc", "wrong-key") is None

    def test_failed_ai_status_is_not_returned_as_cached(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        job = Job(job_id="xyz", url="https://www.upwork.com/jobs/~xyz", title="Job")
        store.record_jobs(run1, search, [job], NOW)
        ev = Evaluation(
            job_id="xyz", excluded=False, exclusion_reasons=[], rule_score=10, final_score=10,
            passed=False, recommendation="skip", reasons=[], risks=[], components={},
            ai_status="failed", ai_code="failed_x", ai_message="err", ai_cache_key="k1",
        )
        store.save_evaluation(run1, ev, job.content_hash(), NOW)

        assert store.cached_ai("xyz", "k1") is None

    def test_cached_status_is_returned(self, store: Storage):
        run1 = store.start_run(NOW)
        search = make_search()
        store.upsert_searches([search], NOW)
        job = Job(job_id="c1", url="https://www.upwork.com/jobs/~c1", title="Job")
        store.record_jobs(run1, search, [job], NOW)
        ev = Evaluation(
            job_id="c1", excluded=False, exclusion_reasons=[], rule_score=50, final_score=50,
            passed=True, recommendation="review", reasons=[], risks=[], components={},
            ai_status="cached", ai_code="cached", ai_relevance=70, ai_recommendation="review",
            ai_cache_key="k1",
        )
        store.save_evaluation(run1, ev, job.content_hash(), NOW)

        assert store.cached_ai("c1", "k1") is not None


# A hand-built schema v1 database: the current tables minus the columns/index added in v2
# (jobs.details_attempts, jobs.details_last_error, jobs.details_pending, evaluations.ai_cache_key),
# so that opening it with today's Storage must upgrade it in place rather than silently ignoring
# old rows.
_V1_SCHEMA = """
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    abort_reason    TEXT,
    searches_found  INTEGER NOT NULL DEFAULT 0,
    report_path     TEXT
);
CREATE TABLE searches (
    search_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE TABLE search_runs (
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    search_id       TEXT NOT NULL REFERENCES searches(search_id),
    position        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    cards_found     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    PRIMARY KEY (run_id, search_id)
);
CREATE TABLE jobs (
    job_id              TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    title               TEXT,
    description         TEXT,
    skills              TEXT NOT NULL DEFAULT '[]',
    posted_text         TEXT,
    posted_at           TEXT,
    job_type            TEXT,
    budget              REAL,
    hourly_min          REAL,
    hourly_max          REAL,
    budget_text         TEXT,
    experience_level    TEXT,
    duration            TEXT,
    proposals_text      TEXT,
    proposals_min       INTEGER,
    proposals_max       INTEGER,
    payment_verified    INTEGER,
    client_rating       REAL,
    client_spend        REAL,
    client_spend_text   TEXT,
    client_country      TEXT,
    details_fetched_at  TEXT,
    first_seen_run_id   INTEGER NOT NULL,
    last_seen_run_id    INTEGER NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);
CREATE TABLE job_searches (
    job_id              TEXT NOT NULL REFERENCES jobs(job_id),
    search_id           TEXT NOT NULL REFERENCES searches(search_id),
    first_seen_run_id   INTEGER NOT NULL,
    last_seen_run_id    INTEGER NOT NULL,
    PRIMARY KEY (job_id, search_id)
);
CREATE TABLE sightings (
    run_id      INTEGER NOT NULL,
    job_id      TEXT NOT NULL,
    search_id   TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id, search_id)
);
CREATE TABLE evaluations (
    run_id              INTEGER NOT NULL,
    job_id              TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    excluded            INTEGER NOT NULL,
    exclusion_reasons   TEXT NOT NULL,
    rule_score          INTEGER NOT NULL,
    final_score         INTEGER NOT NULL,
    passed              INTEGER NOT NULL,
    recommendation      TEXT NOT NULL,
    reasons             TEXT NOT NULL,
    risks               TEXT NOT NULL,
    components          TEXT NOT NULL,
    ai_status           TEXT,
    ai_code             TEXT,
    ai_message          TEXT,
    ai_relevance        INTEGER,
    ai_recommendation   TEXT,
    ai_recommendation_p REAL,
    ai_risks            TEXT,
    ai_explanation      TEXT,
    ai_model            TEXT,
    evaluated_at        TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);
CREATE INDEX idx_sightings_run ON sightings(run_id);
CREATE INDEX idx_eval_job ON evaluations(job_id, content_hash);
"""


def _build_v1_database(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_V1_SCHEMA)
        conn.execute(
            "INSERT INTO runs (id, started_at, status, searches_found) VALUES (1, ?, 'ok', 1)",
            (NOW.isoformat(timespec="seconds"),),
        )
        conn.execute(
            """INSERT INTO jobs (job_id, url, title, skills, first_seen_run_id, last_seen_run_id,
                                 first_seen_at, last_seen_at)
               VALUES ('abc', 'https://www.upwork.com/jobs/~abc', 'Old job', '[]', 1, 1, ?, ?)""",
            (NOW.isoformat(timespec="seconds"), NOW.isoformat(timespec="seconds")),
        )
        conn.execute(
            """INSERT INTO evaluations (run_id, job_id, content_hash, excluded, exclusion_reasons,
                   rule_score, final_score, passed, recommendation, reasons, risks, components, evaluated_at)
               VALUES (1, 'abc', 'oldhash', 0, '[]', 50, 50, 1, 'review', '[]', '[]', '{}', ?)""",
            (NOW.isoformat(timespec="seconds"),),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


class TestSchemaMigrationV1ToV2:
    def test_opening_a_v1_database_upgrades_it_in_place(self, tmp_path: Path):
        db_path = tmp_path / "legacy.sqlite3"
        _build_v1_database(db_path)

        store = Storage(db_path)
        try:
            jobs_columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(jobs)")}
            eval_columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(evaluations)")}
            assert {"details_attempts", "details_last_error", "details_pending"} <= jobs_columns
            assert "ai_cache_key" in eval_columns

            version = store.conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 2

            # The pre-existing rows survived the upgrade.
            assert store.get_run(1) is not None
            assert store.count_jobs() == 1
            rec = store.get_record("abc")
            assert rec is not None
            assert rec.job.title == "Old job"
            assert rec.details_attempts == 0
            assert rec.details_pending is False
        finally:
            store.close()

    def test_reopening_an_already_migrated_database_is_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "legacy.sqlite3"
        _build_v1_database(db_path)

        first = Storage(db_path)
        first.close()

        second = Storage(db_path)
        try:
            version = second.conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 2
            assert second.count_jobs() == 1
            rec = second.get_record("abc")
            assert rec is not None
            assert rec.details_attempts == 0
            assert rec.details_pending is False
        finally:
            second.close()
