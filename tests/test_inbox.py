"""Tests for upwork_scout.inbox: parsing pages the user saved manually from Chrome.

No network, no browser: .mhtml files are built in-memory with :func:`build_mhtml` (a minimal
Chrome-like multipart/related message), mirroring what Ctrl+S -> "Webpage, Single File" produces.
"""

from __future__ import annotations

from pathlib import Path

from upwork_scout.inbox import (
    JOB_PAGES_SEARCH,
    SavedPage,
    identify_search,
    process_inbox,
    read_saved_page,
)
from upwork_scout.storage import Storage

from .conftest import NOW, build_mhtml, fixture_html

JOB_URL = "https://www.upwork.com/jobs/~01c9d8e7f6a5b4c3d2"


class TestReadSavedPageMhtml:
    def test_url_and_html_decoded_cyrillic_survives(self, tmp_path: Path):
        html = ("<html><head><title>Тест</title></head><body>"
                "<p>Кириллица работает</p>"
                "</body></html>")
        path = tmp_path / "page.mhtml"
        build_mhtml(path, "https://www.upwork.com/nx/find-work/9860550", html)

        page = read_saved_page(path)

        assert page.url == "https://www.upwork.com/nx/find-work/9860550"
        assert "Кириллица работает" in page.html

    def test_part_without_charset_is_utf8_not_replacement_chars(self, tmp_path: Path):
        # Chrome writes "Content-Type: text/html" with no charset; email's get_content() then
        # assumes US-ASCII and every UTF-8 byte of "—" or "→" became U+FFFD.
        html = "<html><body><h2>Retell AI Voice Agent — Fix</h2><p>email → WhatsApp</p></body></html>"
        path = tmp_path / "page.mhtml"
        build_mhtml(path, "https://www.upwork.com/nx/find-work/9860550", html)

        page = read_saved_page(path)

        assert "Retell AI Voice Agent — Fix" in page.html
        assert "email → WhatsApp" in page.html
        assert "�" not in page.html

    def test_explicit_charset_is_respected(self, tmp_path: Path):
        path = tmp_path / "page.mhtml"
        build_mhtml(path, "https://www.upwork.com/nx/find-work/9860550",
                    "<html><body><p>Кириллица — да</p></body></html>", charset="utf-8")

        assert "Кириллица — да" in read_saved_page(path).html


class TestReadSavedPageHtml:
    def test_saved_from_url_comment_on_first_line(self, tmp_path: Path):
        content = ("<!-- saved from url=(0048)https://www.upwork.com/nx/find-work/9860550 -->\n"
                  "<html><head><title>t</title></head><body><p>x</p></body></html>")
        path = tmp_path / "page.html"
        path.write_text(content, encoding="utf-8")

        page = read_saved_page(path)
        assert page.url == "https://www.upwork.com/nx/find-work/9860550"

    def test_canonical_link_used_when_no_saved_from_comment(self, tmp_path: Path):
        content = ('<html><head><link rel="canonical" '
                  'href="https://www.upwork.com/nx/find-work/9860554"></head><body><p>x</p></body></html>')
        path = tmp_path / "page.html"
        path.write_text(content, encoding="utf-8")

        page = read_saved_page(path)
        assert page.url == "https://www.upwork.com/nx/find-work/9860554"


class TestIdentifySearch:
    def test_name_from_the_pages_own_tab_link(self):
        page = SavedPage(Path("a.html"), "https://www.upwork.com/nx/find-work/9860550",
                         fixture_html("search_feed_legacy.html"))
        search = identify_search(page)
        assert search.search_id == "9860550"
        assert search.name == "telegram bot"

    def test_falls_back_to_page_title_when_no_matching_tab_link(self):
        # search_feed_jobtile.html has no /nx/find-work/9860538 tab link of its own.
        page = SavedPage(Path("b.html"), "https://www.upwork.com/nx/find-work/9860538",
                         fixture_html("search_feed_jobtile.html"))
        search = identify_search(page)
        assert search.search_id == "9860538"
        assert search.name == "AI Agent"

    def test_non_search_url_gets_a_pseudo_id(self):
        page = SavedPage(Path("c.html"), JOB_URL, fixture_html("job_details.html"))
        search = identify_search(page)
        assert search.search_id.startswith("page-")
        assert search.url == JOB_URL

    def test_no_url_falls_back_to_find_work_url(self):
        page = SavedPage(Path("myfile.html"), None, "<html><head><title>Some Title</title></head><body></body></html>")
        search = identify_search(page)
        assert search.search_id.startswith("page-")
        assert search.name == "Some Title"
        assert search.url == "https://www.upwork.com/nx/find-work/"

    def test_non_upwork_url_falls_back_to_find_work_url(self):
        page = SavedPage(Path("d.html"), "https://example.com/somepage",
                         "<html><head><title>Some Title</title></head><body></body></html>")
        search = identify_search(page)
        assert search.search_id.startswith("page-")
        assert search.url == "https://www.upwork.com/nx/find-work/"


def _seed_inbox(inbox: Path) -> None:
    build_mhtml(inbox / "a-legacy.mhtml", "https://www.upwork.com/nx/find-work/9860550",
               fixture_html("search_feed_legacy.html"))
    build_mhtml(inbox / "b-jobtile.mhtml", "https://www.upwork.com/nx/find-work/9860538",
               fixture_html("search_feed_jobtile.html"))

    job_page = f"<!-- saved from url=(0048){JOB_URL} -->\n{fixture_html('job_details.html')}"
    (inbox / "c-job.html").write_text(job_page, encoding="utf-8")
    (inbox / "c-job_files").mkdir()
    (inbox / "c-job_files" / "asset.css").write_text("body{}", encoding="utf-8")

    cf_page = ("<!-- saved from url=(0033)https://www.upwork.com/nx/find-work/ -->\n"
              + fixture_html("cloudflare_challenge.html"))
    (inbox / "d-cloudflare.html").write_text(cf_page, encoding="utf-8")

    login_page = ("<!-- saved from url=(0033)https://www.upwork.com/nx/find-work/ -->\n"
                 + fixture_html("login_page.html"))
    (inbox / "e-login.html").write_text(login_page, encoding="utf-8")

    build_mhtml(inbox / "f-empty.mhtml", "https://www.upwork.com/nx/find-work/9860530",
               fixture_html("search_empty.html"))

    (inbox / "g-garbage.mhtml").write_bytes(b"not a real mhtml file at all, just garbage bytes 0123456789")


class TestProcessInboxFullScenario:
    def test_counts_and_skips(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        _seed_inbox(inbox)

        run_id = store.start_run(NOW)
        notes: list[str] = []
        result = process_inbox(cfg, store, run_id, NOW, notes.append)

        assert result.files == 7
        assert result.search_pages == 2
        assert result.job_pages == 1
        assert result.processed == 3
        assert len(result.skipped) == 4
        assert any("проверки безопасности" in s for s in result.skipped)
        assert any("страница входа" in s for s in result.skipped)
        # f-empty.mhtml has a real upwork.com address but no job cards; g-garbage.mhtml has no
        # recoverable address at all, so it is rejected for that (stricter, checked first) reason.
        assert sum("карточек вакансий не найдено" in s for s in result.skipped) == 1
        assert sum("нет адреса страницы upwork.com" in s for s in result.skipped) == 1
        assert store.count_jobs() == 5

    def test_job_page_details_are_merged(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        _seed_inbox(inbox)
        run_id = store.start_run(NOW)
        process_inbox(cfg, store, run_id, NOW, lambda m: None)

        rec = store.get_record("01c9d8e7f6a5b4c3d2")
        assert rec is not None
        assert rec.details_fetched_at is not None
        assert rec.job.client_country == "Australia"

    def test_accepted_files_archived_to_processed_including_files_folder(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        _seed_inbox(inbox)
        run_id = store.start_run(NOW)
        result = process_inbox(cfg, store, run_id, NOW, lambda m: None)

        assert result.archive is not None
        archived = {p.name for p in result.archive.iterdir()}
        assert archived == {"a-legacy.mhtml", "b-jobtile.mhtml", "c-job.html", "c-job_files"}

    def test_rejected_files_archived_separately(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        _seed_inbox(inbox)
        run_id = store.start_run(NOW)
        result = process_inbox(cfg, store, run_id, NOW, lambda m: None)

        assert result.rejected_archive is not None
        assert result.rejected_archive != result.archive
        rejected = {p.name for p in result.rejected_archive.iterdir()}
        assert rejected == {"d-cloudflare.html", "e-login.html", "f-empty.mhtml", "g-garbage.mhtml"}
        remaining = {p.name for p in inbox.iterdir()}
        assert remaining == {"processed", "rejected"}

    def test_move_processed_false_leaves_files_in_place(self, store: Storage, cfg):
        cfg.inbox.move_processed = False
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        _seed_inbox(inbox)
        run_id = store.start_run(NOW)
        result = process_inbox(cfg, store, run_id, NOW, lambda m: None)

        assert result.archive is None
        assert result.rejected_archive is None
        remaining = {p.name for p in inbox.iterdir()}
        assert "a-legacy.mhtml" in remaining
        assert "processed" not in remaining
        assert "rejected" not in remaining


class TestProcessInboxFailsClosedOnOrigin:
    """A file whose recovered page address is missing or not on upwork.com must never reach the
    database (or, downstream, the translation API) — only its text was ever "verified" by
    Chrome saving *some* page, not that it really is Upwork's page."""

    def test_foreign_origin_job_page_is_skipped_and_nothing_is_stored(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        foreign_url = "https://example.com/jobs/x_~01c9d8e7f6a5b4c3d2"
        page = f"<!-- saved from url=(0040){foreign_url} -->\n{fixture_html('job_details.html')}"
        (inbox / "foreign.html").write_text(page, encoding="utf-8")

        run_id = store.start_run(NOW)
        result = process_inbox(cfg, store, run_id, NOW, lambda m: None)

        assert result.job_pages == 0
        assert result.search_pages == 0
        assert any("нет адреса страницы upwork.com" in s for s in result.skipped)
        assert store.count_jobs() == 0
        assert store.get_record("01c9d8e7f6a5b4c3d2") is None

    def test_page_without_any_recoverable_url_is_skipped_and_nothing_is_stored(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        # No "saved from url" comment and no <link rel=canonical>/<meta og:url> -> url is None.
        (inbox / "nourl.html").write_text(fixture_html("job_details.html"), encoding="utf-8")

        run_id = store.start_run(NOW)
        result = process_inbox(cfg, store, run_id, NOW, lambda m: None)

        assert result.job_pages == 0
        assert result.search_pages == 0
        assert any("нет адреса страницы upwork.com" in s for s in result.skipped)
        assert store.count_jobs() == 0


class TestProcessInboxUnknownJobPage:
    def test_job_page_for_unknown_job_uses_job_pages_search_id(self, store: Storage, cfg):
        inbox = Path(cfg.inbox.folder)
        inbox.mkdir(parents=True, exist_ok=True)
        other_job_url = "https://www.upwork.com/jobs/~01ffffffffffffffff"
        page = f"<!-- saved from url=(0048){other_job_url} -->\n{fixture_html('job_details.html')}"
        (inbox / "unknown-job.html").write_text(page, encoding="utf-8")

        run_id = store.start_run(NOW)
        result = process_inbox(cfg, store, run_id, NOW, lambda m: None)

        assert result.job_pages == 1
        searches = store.searches_for_job("01ffffffffffffffff")
        assert searches == [(JOB_PAGES_SEARCH.search_id, JOB_PAGES_SEARCH.name)]


def test_identify_search_name_from_active_chip():
    from upwork_scout.inbox import identify_search

    page = SavedPage(Path("x.html"), "https://www.upwork.com/nx/s/find-work/9860550", fixture_html("search_feed_ngm_tile.html"))
    search = identify_search(page)
    assert (search.search_id, search.name) == ("9860550", "telegram bot")
