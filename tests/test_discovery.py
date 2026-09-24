"""Tests for upwork_scout.discovery: saved-search extraction from the Find Work page."""

from __future__ import annotations

from upwork_scout.discovery import parse_saved_searches
from upwork_scout.models import SavedSearch

from .conftest import fixture_html


class TestParseSavedSearches:
    def test_seven_searches_in_page_order(self):
        searches = parse_saved_searches(fixture_html("find_work_saved_searches.html"))
        assert [s.search_id for s in searches] == [
            "9860554", "9860550", "9860547", "9860541", "9860538", "9860534", "9860530",
        ]

    def test_names(self):
        searches = {s.search_id: s.name for s in parse_saved_searches(fixture_html("find_work_saved_searches.html"))}
        assert searches["9860554"] == "wordpress developer"
        assert searches["9860550"] == "telegram bot"
        assert searches["9860547"] == "Python Automation"
        assert searches["9860541"] == "api integrations"
        assert searches["9860538"] == "AI Agent"
        assert searches["9860534"] == "n8n automation"

    def test_aria_label_fallback_for_icon_only_link(self):
        searches = {s.search_id: s.name for s in parse_saved_searches(fixture_html("find_work_saved_searches.html"))}
        assert searches["9860530"] == "AI Automation"

    def test_urls_are_canonical(self):
        searches = {s.search_id: s.url for s in parse_saved_searches(fixture_html("find_work_saved_searches.html"))}
        assert searches["9860554"] == "https://www.upwork.com/nx/find-work/9860554"

    def test_no_duplicates(self):
        searches = parse_saved_searches(fixture_html("find_work_saved_searches.html"))
        ids = [s.search_id for s in searches]
        assert len(ids) == len(set(ids))

    def test_distractors_excluded(self):
        searches = parse_saved_searches(fixture_html("find_work_saved_searches.html"))
        ids = {s.search_id for s in searches}
        # best-matches/most-recent/domestic/saved-jobs tabs, a job-inside-search link and a
        # foreign-host link with the same path must never become a saved search.
        assert "best-matches" not in ids
        assert "most-recent" not in ids
        assert "domestic" not in ids
        assert "saved-jobs" not in ids
        assert len(ids) == 7

    def test_returns_saved_search_dataclass(self):
        searches = parse_saved_searches(fixture_html("find_work_saved_searches.html"))
        assert all(isinstance(s, SavedSearch) for s in searches)

    def test_no_searches_fixture_returns_empty_list(self):
        assert parse_saved_searches(fixture_html("find_work_no_searches.html")) == []
