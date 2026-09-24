"""Tests for upwork_scout.report: HTML rendering, escaping and report file naming."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from upwork_scout.config import load_config
from upwork_scout.models import Job
from upwork_scout.report import ReportContext, build_item, render_report, report_path, write_report
from upwork_scout.scoring import Scorer

from .conftest import NOW

CFG = load_config()


def make_item(job_id="021840000000000000002", title='AI agent <script>alert(1)</script> for support "tickets"',
             **job_kwargs):
    scorer = Scorer(CFG.filters, CFG.scoring)
    job = Job(job_id=job_id, url=f"https://www.upwork.com/jobs/~{job_id}", title=title,
              job_type="fixed", budget=800.0, **job_kwargs)
    ev = scorer.evaluate(job, NOW)
    return build_item(job, ev, True, ["Search A"], NOW, CFG.scoring)


class TestEscaping:
    def test_xss_title_appears_only_escaped(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[make_item()])
        html = render_report(ctx)
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "<script>alert" not in html


class TestJobLinks:
    def test_every_job_href_starts_with_canonical_job_url(self):
        items = [make_item(job_id="01f4a3b2c1d0e9f8a7"), make_item(job_id="01b1c2d3e4f5a6b7c8")]
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=items)
        html = render_report(ctx)
        for item in items:
            assert item["url"].startswith("https://www.upwork.com/jobs/~")
            assert item["url"] in html


class TestEmptyStates:
    def test_show_new_empty_main_message(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[], new_jobs=0)
        html = render_report(ctx)
        assert "Подходящих новых вакансий нет" in html

    def test_show_all_empty_main_message(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="all", min_score=40, main=[], unique_jobs=0)
        html = render_report(ctx)
        assert "Подходящих вакансий нет" in html
        assert "Подходящих новых вакансий нет" not in html


class TestAbortBanner:
    def test_abort_reason_rendered(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[],
                            abort_reason="Upwork показал CAPTCHA.")
        html = render_report(ctx)
        assert "Проход остановлен." in html
        assert "Upwork показал CAPTCHA." in html

    def test_no_banner_without_abort_reason(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[])
        html = render_report(ctx)
        assert "Проход остановлен." not in html


class TestReportPath:
    def test_default_name(self, tmp_path: Path):
        now_local = datetime(2026, 9, 24, 15, 30)
        path = report_path(tmp_path, now_local)
        assert path.name == "upwork-report-2026-09-24-15-30.html"

    def test_collision_adds_suffix(self, tmp_path: Path):
        now_local = datetime(2026, 9, 24, 15, 30)
        first = report_path(tmp_path, now_local)
        first.write_text("x", encoding="utf-8")
        second = report_path(tmp_path, now_local)
        assert second.name == "upwork-report-2026-09-24-15-30-2.html"


class TestWriteReport:
    def test_writes_utf8(self, tmp_path: Path):
        ctx = ReportContext(run_id=1, generated_at=datetime(2026, 9, 24, 15, 30), show_mode="new",
                            min_score=40, main=[make_item()])
        path = write_report(ctx, tmp_path)
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "Отбор вакансий Upwork" in text
        assert "&lt;script&gt;" in text
