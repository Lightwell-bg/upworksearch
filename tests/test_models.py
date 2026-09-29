"""Tests for upwork_scout.models.Job.merged_with: combining two observations of one job."""

from __future__ import annotations

from upwork_scout.models import Job

BROKEN = "Voice Agent ��� Integration fix, long enough text"
CLEAN = "Voice Agent — Integration fix, long enough text"


def job(**kw) -> Job:
    return Job(job_id="0100000000000000a1", url="https://www.upwork.com/jobs/~0100000000000000a1", **kw)


class TestMergedWith:
    def test_longer_description_wins(self):
        merged = job(description="short").merged_with(job(description="a much longer description"))
        assert merged.description == "a much longer description"
        merged = job(description="a much longer description").merged_with(job(description="short"))
        assert merged.description == "a much longer description"

    def test_clean_description_replaces_mis_decoded_one_even_if_shorter(self):
        assert job(description=BROKEN).merged_with(job(description=CLEAN)).description == CLEAN

    def test_mis_decoded_description_never_replaces_clean_one(self):
        assert job(description=CLEAN).merged_with(job(description=BROKEN + " and more")).description == CLEAN

    def test_newer_title_wins(self):
        assert job(title="A ��� B").merged_with(job(title="A — B")).title == "A — B"

    def test_skills_united_and_broken_ones_dropped_when_new_skills_arrive(self):
        merged = job(skills=["Python", "AI ���"]).merged_with(job(skills=["python", "n8n"]))
        assert merged.skills == ["Python", "n8n"]

    def test_skills_kept_when_newer_has_none(self):
        assert job(skills=["Python"]).merged_with(job(skills=[])).skills == ["Python"]
