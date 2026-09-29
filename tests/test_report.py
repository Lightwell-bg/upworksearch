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
             translation=None, **job_kwargs):
    scorer = Scorer(CFG.filters, CFG.scoring)
    job = Job(job_id=job_id, url=f"https://www.upwork.com/jobs/~{job_id}", title=title,
              job_type="fixed", budget=800.0, **job_kwargs)
    ev = scorer.evaluate(job, NOW)
    return build_item(job, ev, True, ["Search A"], NOW, CFG.scoring, translation)


class TestEscaping:
    def test_xss_title_appears_only_escaped(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[make_item()])
        html = render_report(ctx)
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "<script>alert" not in html


class TestTranslationRendering:
    def test_russian_title_is_escaped_and_original_title_is_shown(self):
        item = make_item(
            job_id="01c9d8e7f6a5b4c3d2",
            title='AI agent <script>alert(1)</script> for support',
            translation=('Агент ИИ <script>alert(2)</script> для поддержки', 'Краткое описание на русском.'),
        )
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[item])
        html = render_report(ctx)

        assert item["title"] == 'Агент ИИ <script>alert(2)</script> для поддержки'
        assert item["title_original"] == 'AI agent <script>alert(1)</script> for support'
        # both the Russian title and the original English title are present, but only escaped.
        assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "<script>alert" not in html
        assert "Краткое описание на русском." in html


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


class TestSortAndFilter:
    def test_item_carries_numeric_sort_keys(self):
        item = make_item(proposals_min=5, proposals_max=10, client_spend=2500.0, client_rating=4.8)
        keys = item["sort"]
        assert keys["score"] == item["final_score"]
        assert keys["fixed"] == 800.0 and keys["hourly"] is None
        assert keys["proposals"] == 10
        assert keys["spend"] == 2500.0 and keys["rating"] == 4.8

    def test_hourly_job_sorts_by_upper_rate_not_fixed_budget(self):
        scorer = Scorer(CFG.filters, CFG.scoring)
        job = Job(job_id="01a1a1a1a1a1a1a1a1", url="https://www.upwork.com/jobs/~01a1a1a1a1a1a1a1a1",
                  title="Python API", job_type="hourly", hourly_min=30.0, hourly_max=60.0)
        item = build_item(job, scorer.evaluate(job, NOW), True, ["S"], NOW, CFG.scoring)
        assert item["sort"]["hourly"] == 60.0 and item["sort"]["fixed"] is None

    def test_rendered_data_attributes_and_search_filter(self):
        a = make_item(job_id="01f4a3b2c1d0e9f8a7")
        b = make_item(job_id="01b1c2d3e4f5a6b7c8")
        b["searches"] = ["python", "Search A"]
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[a], below=[b])
        html = render_report(ctx)
        assert 'id="sort-key"' in html and 'id="f-search"' in html
        # names sorted case-insensitively: python -> 0, Search A -> 1
        assert '<option value="0">python</option>' in html
        assert 'data-s="0 1 "' in html and 'data-s="1 "' in html
        assert 'data-fixed="800.0"' in html
        assert "data-hourly" not in html  # unknown values are omitted, sorted last by the script

    def test_search_filter_hidden_for_single_search(self):
        ctx = ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[make_item()])
        html = render_report(ctx)
        assert 'id="sort-key"' in html and 'id="f-search"' not in html

    def test_no_controls_or_script_without_jobs(self):
        html = render_report(ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40))
        assert 'id="sort-key"' not in html and "<script>" not in html

    def test_search_name_with_markup_is_escaped_in_filter(self):
        item = make_item()
        item["searches"] = ['<img src=x onerror=alert(1)>', "b"]
        html = render_report(ReportContext(run_id=1, generated_at=NOW, show_mode="new", min_score=40, main=[item]))
        assert "<img src=x" not in html
