"""Tests for upwork_scout.textmatch: word-bounded, case-insensitive phrase matching."""

from __future__ import annotations

from upwork_scout.textmatch import compile_phrase, contains_any, find_phrases


class TestWordBoundaries:
    def test_api_matches_rest_api(self):
        assert contains_any("We need a REST API built", ["api"]) is True

    def test_api_does_not_match_rapid(self):
        assert contains_any("Looking for rapid development", ["api"]) is False


class TestPrefixMatch:
    def test_automat_star_matches_automation(self):
        assert contains_any("Zapier automation expert needed", ["automat*"]) is True

    def test_automat_star_matches_automated(self):
        assert contains_any("Build an automated pipeline", ["automat*"]) is True

    def test_automat_star_does_not_match_unrelated_word(self):
        assert contains_any("Automobile detailing service", ["automat*"]) is False


class TestSpacesMatchSeparators:
    def test_space_matches_hyphen(self):
        assert contains_any("Looking for API-integration help", ["api integration"]) is True

    def test_space_matches_underscore(self):
        assert contains_any("api_integration needed", ["api integration"]) is True

    def test_space_matches_slash(self):
        assert contains_any("api/integration work", ["api integration"]) is True


class TestCaseInsensitivity:
    def test_upper_phrase_matches_lower_text(self):
        assert contains_any("this is python work", ["PYTHON"]) is True

    def test_lower_phrase_matches_mixed_case_text(self):
        assert contains_any("Needs FastAPI experience", ["fastapi"]) is True


class TestFindPhrases:
    def test_returns_matched_phrases_only(self):
        found = find_phrases("Python and SEO work", ["python", "seo", "php"])
        assert found == ["python", "seo"]

    def test_empty_text_returns_empty(self):
        assert find_phrases(None, ["python"]) == []
        assert find_phrases("", ["python"]) == []

    def test_blank_phrase_is_ignored(self):
        assert find_phrases("some text", ["   "]) == []


class TestCompilePhraseCaching:
    def test_same_phrase_returns_cached_pattern(self):
        assert compile_phrase("python") is compile_phrase("python")
