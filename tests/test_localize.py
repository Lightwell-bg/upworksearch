"""Tests for upwork_scout.localize: deterministic Russian labels for Upwork's fixed vocabulary."""

from __future__ import annotations

from upwork_scout import localize
from upwork_scout.models import Job


class TestPosted:
    def test_hours_ago(self):
        assert localize.posted("3 hours ago") == "3 ч назад"

    def test_an_hour_ago(self):
        assert localize.posted("an hour ago") == "1 ч назад"

    def test_yesterday(self):
        assert localize.posted("yesterday") == "вчера"

    def test_just_now(self):
        assert localize.posted("just now") == "только что"

    def test_last_week(self):
        assert localize.posted("last week") == "на прошлой неделе"

    def test_absolute_date(self):
        assert localize.posted("Sep 20, 2026") == "20 сен 2026"

    def test_unknown_text_unchanged(self):
        assert localize.posted("some unknown text") == "some unknown text"

    def test_none(self):
        assert localize.posted(None) is None


class TestExperience:
    def test_entry_level(self):
        assert localize.experience("Entry level") == "начальный"

    def test_intermediate(self):
        assert localize.experience("Intermediate") == "средний"

    def test_expert(self):
        assert localize.experience("Expert") == "эксперт"

    def test_none(self):
        assert localize.experience(None) is None

    def test_unknown_unchanged(self):
        assert localize.experience("Something else") == "Something else"


class TestDuration:
    def test_months_and_hours_combo(self):
        assert localize.duration("1 to 3 months, Less than 30 hrs/week") == "1–3 месяца, до 30 ч/нед"

    def test_more_than_6_months(self):
        assert localize.duration("More than 6 months") == "более 6 месяцев"

    def test_hours_to_be_determined(self):
        assert localize.duration("Hours to be determined") == "часы не определены"

    def test_none(self):
        assert localize.duration(None) is None


class TestProposals:
    def test_less_than(self):
        assert localize.proposals("Less than 5") == "меньше 5"

    def test_range(self):
        assert localize.proposals("5 to 10") == "5–10"

    def test_plus_unchanged(self):
        assert localize.proposals("50+") == "50+"

    def test_none(self):
        assert localize.proposals(None) is None


class TestCountry:
    def test_known_country(self):
        assert localize.country("United States") == "США"

    def test_unknown_unchanged(self):
        assert localize.country("Wakanda") == "Wakanda"

    def test_none(self):
        assert localize.country(None) is None


def _job(**kwargs) -> Job:
    kwargs.setdefault("job_id", "j")
    kwargs.setdefault("url", "https://www.upwork.com/jobs/~j")
    return Job(**kwargs)


class TestBudget:
    def test_fixed_with_amount(self):
        assert localize.budget(_job(job_type="fixed", budget=500.0)) == "фикс. цена $500"

    def test_fixed_without_amount(self):
        assert localize.budget(_job(job_type="fixed", budget=None)) == "фикс. цена, бюджет не указан"

    def test_hourly_range(self):
        job = _job(job_type="hourly", hourly_min=20.0, hourly_max=40.0)
        assert localize.budget(job) == "почасовая $20–$40/ч"

    def test_hourly_without_rate(self):
        job = _job(job_type="hourly", hourly_min=None, hourly_max=None)
        assert localize.budget(job) == "почасовая, ставка не указана"

    def test_hourly_single_rate(self):
        job = _job(job_type="hourly", hourly_min=25.0, hourly_max=25.0)
        assert localize.budget(job) == "почасовая $25/ч"

    def test_unknown_job_type(self):
        assert localize.budget(_job()) == "не указано"
