"""Tests for upwork_scout.app: CLI argument parsing/overrides and the inbox entry point.

run_inbox is exercised with ai.enabled=False and translate.enabled=False so neither optional
stage makes an HTTP call (no network, no monkeypatching needed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import upwork_scout.app as app_module
from upwork_scout.app import EXIT_FAILED, EXIT_OK, apply_overrides, parse_args, run_inbox
from upwork_scout.config import Secrets, load_config
from upwork_scout.models import Job, SavedSearch
from upwork_scout.storage import Storage

from .conftest import NOW, build_mhtml, fixture_html


class TestParseArgsAndOverrides:
    def test_inbox_flag_sets_source_inbox(self):
        args = parse_args(["--inbox"])
        cfg = load_config()
        apply_overrides(cfg, args)
        assert cfg.collection.source == "inbox"

    def test_browser_flag_sets_source_browser(self):
        args = parse_args(["--browser"])
        cfg = load_config()
        apply_overrides(cfg, args)
        assert cfg.collection.source == "browser"

    def test_inbox_and_browser_together_is_a_system_exit(self):
        with pytest.raises(SystemExit):
            parse_args(["--inbox", "--browser"])

    def test_no_translate_disables_translate(self):
        args = parse_args(["--no-translate"])
        cfg = load_config()
        assert cfg.translate.enabled is True
        apply_overrides(cfg, args)
        assert cfg.translate.enabled is False

    def test_neither_flag_leaves_source_unchanged(self):
        args = parse_args([])
        cfg = load_config()
        apply_overrides(cfg, args)
        assert cfg.collection.source == "browser"  # config.yaml default


class TestRunInbox:
    def _secrets(self) -> Secrets:
        return Secrets(openrouter_api_key=None, jev_model="m", jev_api_url="https://x/y")

    def test_empty_inbox_returns_failed_and_creates_no_run(self, store: Storage, cfg):
        cfg.ai.enabled = False
        cfg.translate.enabled = False
        Path(cfg.inbox.folder).mkdir(parents=True, exist_ok=True)

        rc = run_inbox(cfg, self._secrets(), store)

        assert rc == EXIT_FAILED
        assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

    def test_inbox_with_files_returns_ok_and_writes_a_report(self, store: Storage, cfg):
        cfg.ai.enabled = False
        cfg.translate.enabled = False
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        build_mhtml(inbox / "a.mhtml", "https://www.upwork.com/nx/find-work/9860550",
                   fixture_html("search_feed_legacy.html"))

        rc = run_inbox(cfg, self._secrets(), store)

        assert rc == EXIT_OK
        reports = list(Path(cfg.report.output_dir).glob("*.html"))
        assert len(reports) == 1
        assert store.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1

    def test_only_rejected_files_fails_but_still_writes_a_report(self, store: Storage, cfg):
        cfg.ai.enabled = False
        cfg.translate.enabled = False
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        # Not on upwork.com -> rejected outright, nothing merged into any run.
        (inbox / "foreign.html").write_text(
            "<!-- saved from url=(0022)https://example.com/x -->\n<html><body>x</body></html>",
            encoding="utf-8",
        )

        rc = run_inbox(cfg, self._secrets(), store)

        assert rc == EXIT_FAILED
        row = store.conn.execute("SELECT status, abort_reason FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
        assert "Ни один файл" in row["abort_reason"]
        reports = list(Path(cfg.report.output_dir).glob("*.html"))
        assert len(reports) == 1  # the report is still written even though the run failed


class TestFinishSurvivesATranslateFailure:
    def test_translate_items_raising_still_writes_a_report(self, store: Storage, cfg, monkeypatch: pytest.MonkeyPatch):
        cfg.ai.enabled = False
        run_id = store.start_run(NOW)
        search = SavedSearch(search_id="1", name="S", url="https://www.upwork.com/nx/find-work/1")
        store.upsert_searches([search], NOW)
        job_id = "01000000000000000001"
        store.record_jobs(run_id, search,
                          [Job(job_id=job_id, url=f"https://www.upwork.com/jobs/~{job_id}", title="x")], NOW)

        def raising_translate_items(*args, **kwargs):
            raise RuntimeError("translate boom")

        monkeypatch.setattr(app_module, "translate_items", raising_translate_items)

        path = app_module._finish(cfg, self._secrets(), store, run_id, NOW, "ok", None, [], None)

        assert Path(path).exists()
        assert store.get_run(run_id)["status"] == "ok"
        text = Path(path).read_text(encoding="utf-8")
        assert "Перевод на русский не выполнен" in text

    def _secrets(self) -> Secrets:
        return Secrets(openrouter_api_key=None, jev_model="m", jev_api_url="https://x/y")
