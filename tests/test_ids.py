"""Tests for upwork_scout.ids: job/search ID normalisation from URLs."""

from __future__ import annotations

import pytest

from upwork_scout.ids import (
    is_find_work_url,
    job_id_from_uid,
    job_url,
    normalize_job_id,
    parse_search_id,
)


class TestNormalizeJobId:
    def test_title_slug_with_query_string(self):
        assert normalize_job_id("/jobs/Title_~01ABC0123456789012/?ref=x") == "01abc0123456789012"

    def test_find_work_details_path(self):
        href = "/nx/find-work/9860554/details/~01abcdef0123456789"
        assert normalize_job_id(href) == "01abcdef0123456789"

    def test_apply_path(self):
        href = "/ab/proposals/job/~01abcdef0123456789/apply/"
        assert normalize_job_id(href) == "01abcdef0123456789"

    def test_bare_with_tilde(self):
        assert normalize_job_id("~01abcdef0123456789") == "01abcdef0123456789"

    def test_bare_without_tilde(self):
        assert normalize_job_id("01abcdef0123456789") == "01abcdef0123456789"

    def test_freelancer_profile_is_not_a_job(self):
        assert normalize_job_id("/freelancers/~01abcdef0123456789") is None

    def test_garbage(self):
        assert normalize_job_id("garbage") is None

    def test_empty_and_none(self):
        assert normalize_job_id("") is None
        assert normalize_job_id(None) is None

    def test_lowercases(self):
        assert normalize_job_id("~01ABCDEF0123456789") == "01abcdef0123456789"


class TestJobIdFromUid:
    def test_valid_uid(self):
        assert job_id_from_uid("123456789012345") == "02123456789012345"

    def test_too_short(self):
        assert job_id_from_uid("12345") is None

    def test_none(self):
        assert job_id_from_uid(None) is None

    def test_too_long(self):
        assert job_id_from_uid("12345678901234567890123456") is None


class TestJobUrl:
    def test_valid_id(self):
        assert job_url("01abcdef0123456789") == "https://www.upwork.com/jobs/~01abcdef0123456789"

    def test_invalid_id_raises(self):
        with pytest.raises(ValueError):
            job_url("garbage")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            job_url("")


class TestParseSearchId:
    def test_relative(self):
        assert parse_search_id("/nx/find-work/9860554") == "9860554"

    def test_absolute_with_trailing_slash(self):
        assert parse_search_id("https://www.upwork.com/nx/find-work/9860554/") == "9860554"

    def test_query_string_ok(self):
        assert parse_search_id("/nx/find-work/9860554/?x=1") == "9860554"

    def test_named_tab_is_not_a_search(self):
        assert parse_search_id("/nx/find-work/best-matches") is None

    def test_job_details_path_is_not_a_search(self):
        assert parse_search_id("/nx/find-work/1/details/~01abcdef0123456789") is None

    def test_foreign_host(self):
        assert parse_search_id("https://evil.example.com/nx/find-work/9860554") is None

    def test_none(self):
        assert parse_search_id(None) is None


class TestIsFindWorkUrl:
    def test_root(self):
        assert is_find_work_url("https://www.upwork.com/nx/find-work/") is True

    def test_with_search_id(self):
        assert is_find_work_url("https://www.upwork.com/nx/find-work/9860554") is True

    def test_job_page_is_not_find_work(self):
        assert is_find_work_url("https://www.upwork.com/jobs/~01abcdef0123456789") is False

    def test_foreign_host(self):
        assert is_find_work_url("https://evil.example.com/nx/find-work/") is False
