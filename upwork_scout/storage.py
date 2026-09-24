"""SQLite storage: runs, saved searches, jobs (one row per job ID), job↔search matches,
sighting history and per-run evaluations.

A job is *new* in a run when its ``first_seen_run_id`` equals that run — i.e. its ID was not
in the database before the run started. Data are committed after every search, so an
aborted pass keeps everything collected up to that point.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Job, SavedSearch
from .scoring import Evaluation

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',   -- running | ok | aborted | failed
    abort_reason    TEXT,
    searches_found  INTEGER NOT NULL DEFAULT 0,
    report_path     TEXT
);
CREATE TABLE IF NOT EXISTS searches (
    search_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_runs (
    run_id          INTEGER NOT NULL REFERENCES runs(id),
    search_id       TEXT NOT NULL REFERENCES searches(search_id),
    position        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,                     -- ok | error | aborted | skipped
    cards_found     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    PRIMARY KEY (run_id, search_id)
);
CREATE TABLE IF NOT EXISTS jobs (
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
    details_attempts    INTEGER NOT NULL DEFAULT 0,
    details_last_error  TEXT,
    details_pending     INTEGER NOT NULL DEFAULT 0,       -- deferred by max_per_run, still wanted
    first_seen_run_id   INTEGER NOT NULL,
    last_seen_run_id    INTEGER NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_searches (
    job_id              TEXT NOT NULL REFERENCES jobs(job_id),
    search_id           TEXT NOT NULL REFERENCES searches(search_id),
    first_seen_run_id   INTEGER NOT NULL,
    last_seen_run_id    INTEGER NOT NULL,
    PRIMARY KEY (job_id, search_id)
);
CREATE TABLE IF NOT EXISTS sightings (
    run_id      INTEGER NOT NULL,
    job_id      TEXT NOT NULL,
    search_id   TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id, search_id)
);
CREATE TABLE IF NOT EXISTS evaluations (
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
    ai_cache_key        TEXT,
    evaluated_at        TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);
"""

# Columns added after schema v1. CREATE TABLE IF NOT EXISTS never alters an existing table,
# so databases created by an older version are upgraded here before indexes are created.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "jobs": [
        ("details_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("details_last_error", "TEXT"),
        ("details_pending", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "evaluations": [("ai_cache_key", "TEXT")],
}
_INDEXES = """
DROP INDEX IF EXISTS idx_eval_job;
CREATE INDEX IF NOT EXISTS idx_sightings_run ON sightings(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_ai_key ON evaluations(job_id, ai_cache_key);
"""

_JOB_COLUMNS = [f.name for f in fields(Job)]


@dataclass
class JobRecord:
    job: Job
    first_seen_run_id: int
    last_seen_run_id: int
    first_seen_at: str
    details_fetched_at: str | None
    details_attempts: int = 0
    details_pending: bool = False


@dataclass
class SearchRunRow:
    search_id: str
    name: str
    url: str
    status: str
    cards_found: int
    error: str | None


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.executescript(_INDEXES)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after v1 to an existing database (idempotent, one transaction)."""
        with self.conn:
            for table, columns in _ADDED_COLUMNS.items():
                existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, declaration in columns:
                    if name not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def close(self) -> None:
        self.conn.close()

    # --- runs -----------------------------------------------------------------------------
    def start_run(self, now: datetime) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_iso(now),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: datetime, status: str, abort_reason: str | None,
                   searches_found: int, report_path: str | None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, abort_reason=?, searches_found=?, report_path=? WHERE id=?",
            (_iso(now), status, abort_reason, searches_found, report_path, run_id),
        )
        self.conn.commit()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def last_run_with_jobs(self) -> int | None:
        row = self.conn.execute("SELECT MAX(run_id) AS r FROM sightings").fetchone()
        return int(row["r"]) if row and row["r"] is not None else None

    # --- searches -------------------------------------------------------------------------
    def upsert_searches(self, searches: Iterable[SavedSearch], now: datetime) -> None:
        ts = _iso(now)
        with self.conn:
            for s in searches:
                self.conn.execute(
                    """INSERT INTO searches (search_id, name, url, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(search_id) DO UPDATE SET name=excluded.name, url=excluded.url,
                                                            last_seen_at=excluded.last_seen_at""",
                    (s.search_id, s.name, s.url, ts, ts),
                )

    def record_search_result(self, run_id: int, search_id: str, position: int, status: str,
                             cards_found: int, error: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO search_runs (run_id, search_id, position, status, cards_found, error)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, search_id) DO UPDATE SET status=excluded.status,
                       cards_found=excluded.cards_found, error=excluded.error""",
                (run_id, search_id, position, status, cards_found, error),
            )

    def search_runs(self, run_id: int) -> list[SearchRunRow]:
        rows = self.conn.execute(
            """SELECT sr.search_id, s.name, s.url, sr.status, sr.cards_found, sr.error
               FROM search_runs sr JOIN searches s ON s.search_id = sr.search_id
               WHERE sr.run_id=? ORDER BY sr.position""",
            (run_id,),
        ).fetchall()
        return [SearchRunRow(**dict(r)) for r in rows]

    def searches_for_job(self, job_id: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            """SELECT s.search_id, s.name FROM job_searches js JOIN searches s ON s.search_id = js.search_id
               WHERE js.job_id=? ORDER BY s.name""",
            (job_id,),
        ).fetchall()
        return [(r["search_id"], r["name"]) for r in rows]

    # --- jobs -----------------------------------------------------------------------------
    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        data: dict[str, Any] = {c: row[c] for c in _JOB_COLUMNS}
        data["skills"] = json.loads(row["skills"] or "[]")
        if data["payment_verified"] is not None:
            data["payment_verified"] = bool(data["payment_verified"])
        return Job(**data)

    def get_record(self, job_id: str) -> JobRecord | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return JobRecord(
            job=self._row_to_job(row),
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            first_seen_at=row["first_seen_at"],
            details_fetched_at=row["details_fetched_at"],
            details_attempts=row["details_attempts"] or 0,
            details_pending=bool(row["details_pending"]),
        )

    def get_job(self, job_id: str) -> Job | None:
        rec = self.get_record(job_id)
        return rec.job if rec else None

    def _write_job(self, job: Job, run_id: int, ts: str, details: bool = False) -> None:
        values = [getattr(job, c) for c in _JOB_COLUMNS]
        values[_JOB_COLUMNS.index("skills")] = json.dumps(job.skills, ensure_ascii=False)
        pv = job.payment_verified
        values[_JOB_COLUMNS.index("payment_verified")] = None if pv is None else int(pv)
        cols = ", ".join(_JOB_COLUMNS)
        placeholders = ", ".join("?" for _ in _JOB_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in _JOB_COLUMNS if c != "job_id")
        details_sql = (", details_fetched_at=excluded.details_fetched_at, details_last_error=NULL, "
                       "details_attempts=details_attempts+1, details_pending=0") if details else ""
        self.conn.execute(
            f"""INSERT INTO jobs ({cols}, details_fetched_at, first_seen_run_id, last_seen_run_id,
                                  first_seen_at, last_seen_at)
                VALUES ({placeholders}, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET {updates}, last_seen_run_id=excluded.last_seen_run_id,
                                                  last_seen_at=excluded.last_seen_at{details_sql}""",
            (*values, ts if details else None, run_id, run_id, ts, ts),
        )

    def record_jobs(self, run_id: int, search: SavedSearch, jobs: Iterable[Job], now: datetime) -> None:
        """Merge the cards of one search into the database (one transaction)."""
        ts = _iso(now)
        with self.conn:
            for job in jobs:
                existing = self.get_job(job.job_id)
                merged = existing.merged_with(job) if existing else job
                self._write_job(merged, run_id, ts)
                self.conn.execute(
                    """INSERT INTO job_searches (job_id, search_id, first_seen_run_id, last_seen_run_id)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(job_id, search_id) DO UPDATE SET last_seen_run_id=excluded.last_seen_run_id""",
                    (job.job_id, search.search_id, run_id, run_id),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO sightings (run_id, job_id, search_id, seen_at) VALUES (?, ?, ?, ?)",
                    (run_id, job.job_id, search.search_id, ts),
                )

    def update_job_details(self, details: Job, run_id: int, now: datetime) -> None:
        """Merge fields from an opened job page into an existing job."""
        existing = self.get_record(details.job_id)
        if existing is None:
            raise KeyError(details.job_id)
        merged = existing.job.merged_with(details)
        with self.conn:
            self._write_job(merged, existing.last_seen_run_id, _iso(now), details=True)

    def record_details_failure(self, job_id: str, error: str) -> None:
        """Count a failed/rejected job-page attempt (details_fetched_at stays NULL → retried)."""
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET details_attempts = details_attempts + 1, details_last_error = ?, "
                "details_pending = 0 WHERE job_id = ?",
                (error[:300], job_id),
            )

    def mark_details_pending(self, job_ids: Iterable[str]) -> None:
        """Remember jobs that needed their page but did not fit into details.max_per_run."""
        with self.conn:
            self.conn.executemany("UPDATE jobs SET details_pending = 1 WHERE job_id = ?", [(j,) for j in job_ids])

    def run_job_ids(self, run_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT job_id, MIN(seen_at) AS first FROM sightings WHERE run_id=? GROUP BY job_id ORDER BY first, job_id",
            (run_id,),
        ).fetchall()
        return [r["job_id"] for r in rows]

    def count_jobs(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    # --- evaluations ----------------------------------------------------------------------
    def save_evaluation(self, run_id: int, ev: Evaluation, content_hash: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO evaluations (run_id, job_id, content_hash, excluded, exclusion_reasons,
                       rule_score, final_score, passed, recommendation, reasons, risks, components,
                       ai_status, ai_code, ai_message, ai_relevance, ai_recommendation, ai_recommendation_p,
                       ai_risks, ai_explanation, ai_model, ai_cache_key, evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, ev.job_id, content_hash, int(ev.excluded),
                    json.dumps(ev.exclusion_reasons, ensure_ascii=False),
                    ev.rule_score, ev.final_score, int(ev.passed), ev.recommendation,
                    json.dumps(ev.reasons, ensure_ascii=False), json.dumps(ev.risks, ensure_ascii=False),
                    json.dumps(ev.components), ev.ai_status, ev.ai_code, ev.ai_message, ev.ai_relevance,
                    ev.ai_recommendation, ev.ai_recommendation_p, json.dumps(ev.ai_risks, ensure_ascii=False),
                    ev.ai_explanation, ev.ai_model, ev.ai_cache_key, _iso(now),
                ),
            )

    def cached_ai(self, job_id: str, ai_cache_key: str) -> sqlite3.Row | None:
        """Latest successful AI assessment made with exactly the same inputs (see
        ai.ai_cache_key: job state, model, questions, thresholds, version)."""
        return self.conn.execute(
            """SELECT ai_relevance, ai_recommendation, ai_recommendation_p, ai_risks, ai_explanation, ai_model
               FROM evaluations
               WHERE job_id=? AND ai_cache_key=? AND ai_status IN ('ok', 'cached')
               ORDER BY run_id DESC LIMIT 1""",
            (job_id, ai_cache_key),
        ).fetchone()
