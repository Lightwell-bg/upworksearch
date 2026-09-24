"""Tests for upwork_scout.parsing: search-card and job-page field extraction."""

from __future__ import annotations

import upwork_scout.parsing as parsing
from upwork_scout.parsing import (
    estimate_posted_at,
    parse_budget,
    parse_job_details,
    parse_proposals,
    parse_search_page,
)

from .conftest import NOW, fixture_html


def _by_id(jobs):
    return {j.job_id: j for j in jobs}


# ------------------------------------------------------------------------------------------
# search_feed_legacy.html
# ------------------------------------------------------------------------------------------
class TestLegacyFeedCard1Hourly:
    """Telegram bot card: complete hourly job."""

    JOB_ID = "01f4a3b2c1d0e9f8a7"

    def job(self):
        return _by_id(parse_search_page(fixture_html("search_feed_legacy.html"), NOW))[self.JOB_ID]

    def test_title(self):
        assert self.job().title == "Telegram bot with Python (aiogram) and Google Sheets"

    def test_job_type_and_hourly_range(self):
        j = self.job()
        assert j.job_type == "hourly"
        assert j.hourly_min == 30.0
        assert j.hourly_max == 60.0
        assert j.budget is None

    def test_experience_and_duration(self):
        j = self.job()
        assert j.experience_level == "Intermediate"
        assert j.duration == "1 to 3 months, Less than 30 hrs/week"

    def test_posted(self):
        j = self.job()
        assert j.posted_text == "2 hours ago"
        assert j.posted_at == "2026-09-24T10:00:00+00:00"

    def test_proposals(self):
        j = self.job()
        assert j.proposals_text == "Less than 5"
        assert j.proposals_min == 0
        assert j.proposals_max == 4

    def test_client_fields(self):
        j = self.job()
        assert j.payment_verified is True
        assert j.client_rating == 4.93
        assert j.client_spend == 10000.0
        assert j.client_spend_text == "$10K+"
        assert j.client_country == "United States"

    def test_skills_deduped_and_noise_filtered(self):
        # 5 token elements on the card (Python, Telegram API, FastAPI, python-dup, "+2") -> 3 unique skills.
        assert self.job().skills == ["Python", "Telegram API", "FastAPI"]

    def test_description_contains_dollar_amount_but_budget_stays_hourly(self):
        j = self.job()
        assert "Budget for this is $50" in j.description
        assert j.budget is None
        assert j.job_type == "hourly"


class TestLegacyFeedCard2FixedPrice:
    JOB_ID = "01b1c2d3e4f5a6b7c8"

    def job(self):
        return _by_id(parse_search_page(fixture_html("search_feed_legacy.html"), NOW))[self.JOB_ID]

    def test_fixed_budget(self):
        j = self.job()
        assert j.job_type == "fixed"
        assert j.budget == 1500.0
        assert j.hourly_min is None and j.hourly_max is None

    def test_payment_unverified(self):
        assert self.job().payment_verified is False

    def test_client_spend_zero(self):
        j = self.job()
        assert j.client_spend == 0.0
        assert j.client_spend_text == "$0"

    def test_proposals_range(self):
        j = self.job()
        assert j.proposals_text == "20 to 50"
        assert j.proposals_min == 20
        assert j.proposals_max == 50

    def test_country(self):
        assert self.job().client_country == "Germany"

    def test_skills(self):
        assert self.job().skills == ["n8n", "API Integration"]


class TestLegacyFeedCard3Sparse:
    JOB_ID = "01c9d8e7f6a5b4c3d2"

    def job(self):
        return _by_id(parse_search_page(fixture_html("search_feed_legacy.html"), NOW))[self.JOB_ID]

    def test_title_and_description_present(self):
        j = self.job()
        assert j.title == "Quick question about my website"
        assert j.description == "Need help."

    def test_all_optional_fields_are_none(self):
        j = self.job()
        for field in (
            "job_type", "budget", "hourly_min", "hourly_max", "budget_text",
            "experience_level", "duration", "posted_text", "posted_at",
            "proposals_text", "proposals_min", "proposals_max", "payment_verified",
            "client_rating", "client_spend", "client_spend_text", "client_country",
        ):
            assert getattr(j, field) is None, field
        assert j.skills == []


class TestLegacyFeedCardCount:
    def test_three_cards(self):
        jobs = parse_search_page(fixture_html("search_feed_legacy.html"), NOW)
        assert len(jobs) == 3


# ------------------------------------------------------------------------------------------
# search_feed_jobtile.html
# ------------------------------------------------------------------------------------------
class TestJobtileFeedCard1MergesWithLegacy:
    JOB_ID = "01f4a3b2c1d0e9f8a7"

    def job(self):
        return _by_id(parse_search_page(fixture_html("search_feed_jobtile.html"), NOW))[self.JOB_ID]

    def test_same_id_as_legacy_card(self):
        assert self.job().job_id == TestLegacyFeedCard1Hourly.JOB_ID

    def test_hourly_range(self):
        j = self.job()
        assert j.job_type == "hourly"
        assert j.hourly_min == 30.0
        assert j.hourly_max == 60.0

    def test_proposals(self):
        j = self.job()
        assert j.proposals_text == "5 to 10"
        assert j.proposals_min == 5
        assert j.proposals_max == 10

    def test_posted(self):
        assert self.job().posted_text == "3 hours ago"


class TestJobtileFeedCard2XssJob:
    JOB_ID = "021840000000000000002"

    def job(self):
        return _by_id(parse_search_page(fixture_html("search_feed_jobtile.html"), NOW))[self.JOB_ID]

    def test_xss_title_kept_as_raw_text(self):
        j = self.job()
        assert j.title == 'AI agent <script>alert(1)</script> for support "tickets"'

    def test_fixed_budget(self):
        j = self.job()
        assert j.job_type == "fixed"
        assert j.budget == 800.0

    def test_rating_zero_becomes_none(self):
        # Upwork shows 0 stars for a client without reviews -> not a real rating.
        assert self.job().client_rating is None

    def test_proposals_fifty_plus(self):
        j = self.job()
        assert j.proposals_text == "50+"
        assert j.proposals_min == 50
        assert j.proposals_max is None

    def test_client_spend(self):
        j = self.job()
        assert j.client_spend == 2500.0
        assert j.client_spend_text == "$2.5K"

    def test_country(self):
        assert self.job().client_country == "Canada"

    def test_skills(self):
        assert self.job().skills == ["LangChain", "OpenAI API"]


class TestJobtileFeedCard3SeoJob:
    JOB_ID = "01d1e2f3a4b5c6d7e8"

    def job(self):
        return _by_id(parse_search_page(fixture_html("search_feed_jobtile.html"), NOW))[self.JOB_ID]

    def test_fixed_low_budget(self):
        j = self.job()
        assert j.job_type == "fixed"
        assert j.budget == 50.0

    def test_proposals(self):
        j = self.job()
        assert j.proposals_text == "15 to 20"
        assert j.proposals_min == 15
        assert j.proposals_max == 20


class TestJobtileFeedCardCount:
    def test_three_unique_cards(self):
        jobs = parse_search_page(fixture_html("search_feed_jobtile.html"), NOW)
        assert len(jobs) == 3


# ------------------------------------------------------------------------------------------
# job_details.html
# ------------------------------------------------------------------------------------------
class TestParseJobDetails:
    JOB_ID = "01c9d8e7f6a5b4c3d2"

    def job(self):
        return parse_job_details(fixture_html("job_details.html"), self.JOB_ID, NOW)

    def test_title(self):
        assert self.job().title == "Quick question about my website"

    def test_fixed_budget(self):
        j = self.job()
        assert j.job_type == "fixed"
        assert j.budget == 250.0

    def test_experience_and_duration(self):
        j = self.job()
        assert j.experience_level == "Intermediate"
        assert j.duration == "Less than 1 month"

    def test_posted(self):
        j = self.job()
        assert j.posted_text == "4 hours ago"
        assert j.posted_at == "2026-09-24T08:00:00+00:00"

    def test_proposals(self):
        j = self.job()
        assert j.proposals_text == "5 to 10"
        assert j.proposals_min == 5
        assert j.proposals_max == 10

    def test_client_block(self):
        j = self.job()
        assert j.payment_verified is True
        assert j.client_rating == 4.8
        assert j.client_spend == 3200.0
        assert j.client_spend_text == "$3.2K"
        assert j.client_country == "Australia"

    def test_skills(self):
        assert self.job().skills == ["WordPress", "PHP", "WooCommerce"]

    def test_description_merges_json_ld_and_page_text(self):
        j = self.job()
        assert "My WordPress site built with WooCommerce is slow." in j.description
        assert "Profile the site" in j.description
        assert "Fix slow queries" in j.description
        assert "Write a report" in j.description

    def test_similar_jobs_block_is_ignored(self):
        j = self.job()
        # The "Similar jobs" block advertises Hourly $5-$10 / 50+ proposals / $5 fixed — none of
        # that must leak into the main job's fields.
        assert j.hourly_min is None and j.hourly_max is None
        assert j.budget == 250.0
        assert j.proposals_min == 5 and j.proposals_max == 10
        assert "Hourly: $5" not in (j.description or "")


# ------------------------------------------------------------------------------------------
# Unit tests: parse_budget
# ------------------------------------------------------------------------------------------
class TestParseBudget:
    def test_hourly_range(self):
        assert parse_budget("Hourly: $30-$60") == ("hourly", None, 30.0, 60.0, "Hourly: $30-$60")

    def test_hourly_word_only(self):
        assert parse_budget("Fixed-price") == ("fixed", None, None, None, "Fixed-price")

    def test_no_match(self):
        assert parse_budget("random text with no money") == (None, None, None, None, None)

    def test_fixed_single_amount(self):
        assert parse_budget("Est. budget: $250.00") == ("fixed", 250.0, None, None, "Est. budget: $250.00")

    def test_hourly_single_rate(self):
        assert parse_budget("Hourly: $45.00") == ("hourly", None, 45.0, 45.0, "Hourly: $45.00")

    def test_k_suffix(self):
        assert parse_budget("Est. budget: $1.5K") == ("fixed", 1500.0, None, None, "Est. budget: $1.5K")


# ------------------------------------------------------------------------------------------
# Unit tests: parse_proposals
# ------------------------------------------------------------------------------------------
class TestParseProposals:
    def test_less_than(self):
        assert parse_proposals("Proposals: Less than 5") == ("Less than 5", 0, 4)

    def test_range(self):
        assert parse_proposals("Proposals: 20 to 50") == ("20 to 50", 20, 50)

    def test_plus(self):
        assert parse_proposals("50+", labeled=False) == ("50+", 50, None)

    def test_bare_number(self):
        assert parse_proposals("Proposals: 12", labeled=True) == ("12", 12, 12)

    def test_duration_like_text_not_read_as_proposals_when_labeled(self):
        # "1 to 3 months" must not be mistaken for a proposals count when it is not
        # actually preceded by a "Proposals:" label.
        assert parse_proposals("1 to 3 months", labeled=True) == (None, None, None)

    def test_duration_like_text_is_read_when_unlabeled(self):
        # labeled=False is only meant to be used on the text of a dedicated proposals
        # element, where such a false positive would not normally occur.
        assert parse_proposals("1 to 3 months", labeled=False) == ("1 to 3", 1, 3)

    def test_none_text(self):
        assert parse_proposals(None) == (None, None, None)

    def test_empty_text(self):
        assert parse_proposals("") == (None, None, None)


# ------------------------------------------------------------------------------------------
# Unit tests: estimate_posted_at
# ------------------------------------------------------------------------------------------
class TestEstimatePostedAt:
    def test_hours_ago(self):
        result = estimate_posted_at("2 hours ago", NOW)
        assert result.isoformat(timespec="seconds") == "2026-09-24T10:00:00+00:00"

    def test_yesterday(self):
        result = estimate_posted_at("yesterday", NOW)
        assert result.isoformat(timespec="seconds") == "2026-09-23T12:00:00+00:00"

    def test_absolute_date(self):
        result = estimate_posted_at("Sep 20, 2026", NOW)
        assert result.isoformat(timespec="seconds") == "2026-09-20T00:00:00+00:00"

    def test_garbage_returns_none(self):
        assert estimate_posted_at("garbage", NOW) is None

    def test_none_text_returns_none(self):
        assert estimate_posted_at(None, NOW) is None


# ------------------------------------------------------------------------------------------
# Resilience: one broken card must not lose the others
# ------------------------------------------------------------------------------------------
class TestCardExtractionIsolatesFailures:
    def test_one_raising_card_does_not_lose_others(self, monkeypatch):
        real_extract = parsing._extract
        broken_id = "01c9d8e7f6a5b4c3d2"

        def flaky_extract(job, root, title_el, now, skip=()):
            if job.job_id == broken_id:
                raise RuntimeError("boom")
            return real_extract(job, root, title_el, now, skip=skip)

        monkeypatch.setattr(parsing, "_extract", flaky_extract)
        jobs = parse_search_page(fixture_html("search_feed_legacy.html"), NOW)
        ids = {j.job_id for j in jobs}
        assert broken_id not in ids
        assert "01f4a3b2c1d0e9f8a7" in ids
        assert "01b1c2d3e4f5a6b7c8" in ids
        assert len(jobs) == 2
