"""Partial-failure regressions from the Codex review of the inbox/translation features:
the translation client lifecycle and file archiving must never cost the report or the run."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import upwork_scout.app as app
import upwork_scout.inbox as inbox_mod
from upwork_scout.config import Secrets
from upwork_scout.translate import Translator
from tests.conftest import build_mhtml, fixture_html

SECRETS = Secrets(openrouter_api_key=None, jev_model="~typesafe/jev-latest",
                  jev_api_url="https://openrouter.ai/api/v1/systemone")


def _inbox_with_two_searches(cfg) -> Path:
    folder = Path(cfg.inbox.folder)
    folder.mkdir(parents=True, exist_ok=True)
    build_mhtml(folder / "a.mhtml", "https://www.upwork.com/nx/find-work/9860550", fixture_html("search_feed_legacy.html"))
    build_mhtml(folder / "b.mhtml", "https://www.upwork.com/nx/find-work/9860538", fixture_html("search_feed_jobtile.html"))
    return folder


def _last_run(store):
    return store.conn.execute("SELECT status, report_path FROM runs ORDER BY id DESC LIMIT 1").fetchone()


class TestTranslatorLifecycle:
    def test_disabled_translator_creates_no_http_client(self, cfg):
        cfg.translate.enabled = False
        t = Translator(cfg.translate, SECRETS)
        assert t._http is None
        t.close()  # must not fail without a client

    def test_close_errors_are_swallowed(self, cfg):
        class BadClient:
            def close(self):
                raise RuntimeError("transport close failed")

        Translator(cfg.translate, SECRETS, http=BadClient()).close()

    def test_translator_construction_failure_still_writes_report(self, store, cfg, monkeypatch):
        cfg.ai.enabled = False
        _inbox_with_two_searches(cfg)

        def boom(*a, **k):
            raise ValueError("malformed HTTPS_PROXY")

        monkeypatch.setattr(app, "Translator", boom)
        assert app.run_inbox(cfg, SECRETS, store) == app.EXIT_OK
        row = _last_run(store)
        assert row["status"] == "ok" and Path(row["report_path"]).is_file()
        assert "Перевод на русский не выполнен" in Path(row["report_path"]).read_text(encoding="utf-8")


class TestArchiveFailuresAreIsolated:
    def test_locked_second_file_keeps_run_ok_and_is_reported(self, store, cfg, monkeypatch):
        cfg.ai.enabled = False
        cfg.translate.enabled = False
        folder = _inbox_with_two_searches(cfg)
        real_move = shutil.move

        def flaky_move(src, dst):
            if Path(src).name == "b.mhtml":
                raise PermissionError("file is locked")
            return real_move(src, dst)

        monkeypatch.setattr(inbox_mod.shutil, "move", flaky_move)
        assert app.run_inbox(cfg, SECRETS, store) == app.EXIT_OK

        row = _last_run(store)
        assert row["status"] == "ok"
        report = Path(row["report_path"]).read_text(encoding="utf-8")
        assert "b.mhtml" in report and "не удалось перенести" in report
        assert (folder / "b.mhtml").exists()  # locked file stays in the inbox
        processed = list((folder / "processed").rglob("a.mhtml"))
        assert len(processed) == 1
        # both searches' jobs were ingested and are new in this run
        assert store.count_jobs() == 5

    def test_archive_folder_creation_failure_keeps_run_ok(self, store, cfg, monkeypatch):
        cfg.ai.enabled = False
        cfg.translate.enabled = False
        folder = _inbox_with_two_searches(cfg)
        real_mkdir = Path.mkdir

        def failing_mkdir(self, *a, **k):
            if self.parent.name in ("processed", "rejected"):
                raise PermissionError("read-only")
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        assert app.run_inbox(cfg, SECRETS, store) == app.EXIT_OK
        row = _last_run(store)
        assert row["status"] == "ok"
        assert "не удалось создать папку" in Path(row["report_path"]).read_text(encoding="utf-8")
        assert (folder / "a.mhtml").exists() and (folder / "b.mhtml").exists()
        assert store.count_jobs() == 5

    def test_archive_never_overwrites_existing_file(self, tmp_path):
        from datetime import datetime, timezone

        folder = tmp_path / "inbox"
        folder.mkdir()
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        for content in ("first", "second"):
            (folder / "same.mhtml").write_text(content, encoding="utf-8")
            target = inbox_mod._archive([folder / "same.mhtml"], folder, now)
        names = sorted(p.name for p in target.iterdir())
        assert names == ["same (2).mhtml", "same.mhtml"]
        assert (target / "same.mhtml").read_text(encoding="utf-8") == "first"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """These tests never talk to OpenRouter."""
    import httpx

    def refuse(*a, **k):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(httpx.Client, "post", refuse)
