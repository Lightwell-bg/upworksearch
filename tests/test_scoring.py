"""Tests for upwork_scout.scoring: hard exclusion rules and the explainable 0-100 score."""

from __future__ import annotations

from upwork_scout.ai import AIOutcome, AIResult
from upwork_scout.config import load_config
from upwork_scout.models import Job
from upwork_scout.scoring import Scorer, apply_ai

from .conftest import NOW


def scorer():
    cfg = load_config()
    return Scorer(cfg.filters, cfg.scoring), cfg


def job(**kwargs):
    kwargs.setdefault("job_id", "j")
    kwargs.setdefault("url", "https://www.upwork.com/jobs/~j")
    return Job(**kwargs)


class TestHardExclusions:
    def test_fixed_budget_too_low_excluded(self):
        s, _ = scorer()
        ev = s.evaluate(job(title="Python dev", job_type="fixed", budget=50.0), NOW)
        assert ev.excluded is True
        assert any("50" in r and ("бюджет" in r or "budget" in r.lower()) for r in ev.exclusion_reasons)

    def test_hourly_max_too_low_excluded(self):
        s, _ = scorer()
        ev = s.evaluate(job(title="dev", job_type="hourly", hourly_min=8.0, hourly_max=10.0), NOW)
        assert ev.excluded is True
        assert any("10" in r for r in ev.exclusion_reasons)

    def test_hourly_no_rate_not_excluded(self):
        s, _ = scorer()
        ev = s.evaluate(job(title="dev", job_type="hourly"), NOW)
        assert ev.excluded is False

    def test_seo_in_title_excluded(self):
        s, _ = scorer()
        ev = s.evaluate(job(title="Need SEO expert", job_type="fixed", budget=500.0), NOW)
        assert ev.excluded is True
        assert any("seo" in r.lower() for r in ev.exclusion_reasons)

    def test_marketing_automation_title_is_softened_not_excluded(self):
        s, _ = scorer()
        ev = s.evaluate(job(title="Marketing automation with n8n", job_type="fixed", budget=500.0,
                            skills=["n8n"]), NOW)
        assert ev.excluded is False
        assert any("marketing" in r.lower() and "−" in r for r in ev.risks)


class TestDescriptionOnlyPenaltyIsHalved:
    BASE_DESC = ("We build custom internal tools for clients on a long term basis with a dedicated "
                 "team of specialists located here and there, working across several time zones "
                 "every single week.")
    WITH_MARKETING = BASE_DESC + " Some marketing work is also involved occasionally as part of this role."

    def test_half_penalty_lowers_rule_score(self):
        s, _ = scorer()
        base = s.evaluate(job(title="Custom project", job_type="fixed", budget=500.0,
                              description=self.BASE_DESC), NOW)
        with_marketing = s.evaluate(job(title="Custom project", job_type="fixed", budget=500.0,
                                        description=self.WITH_MARKETING), NOW)
        assert base.rule_score == 28
        assert with_marketing.rule_score == 16
        assert with_marketing.rule_score < base.rule_score
        assert not any("marketing" in r.lower() for r in base.risks)
        assert any("marketing" in r.lower() and "−" in r for r in with_marketing.risks)


class TestUnknownFieldsAreNeutral:
    def test_all_optional_fields_none_gives_neutral_components(self):
        s, cfg = scorer()
        ev = s.evaluate(job(title="dev"), NOW)
        unknown = cfg.scoring.unknown_share
        assert ev.components["freshness"] == cfg.scoring.freshness.max_points * unknown
        assert ev.components["competition"] == cfg.scoring.competition.max_points * unknown
        assert ev.components["budget"] == cfg.scoring.budget.max_points * unknown
        assert ev.components["clarity"] == cfg.scoring.clarity.max_points * unknown
        expected_client = unknown * (
            cfg.scoring.client.payment_verified_points
            + cfg.scoring.client.spend_points
            + cfg.scoring.client.rating_points
        )
        assert ev.components["client"] == expected_client
        for key in ("freshness", "competition", "budget", "clarity", "client"):
            assert ev.components[key] > 0


class TestStrongCandidate:
    def job_strong(self):
        return job(
            title="Python FastAPI developer needed",
            description=(
                "We need a Python FastAPI developer to build REST API integrations with webhooks. "
                "Deliverables: endpoint specification, tech stack documentation, and acceptance criteria. "
                "The goal is to have a working prototype within 2 weeks. Milestone 1: API design. "
                "Milestone 2: implementation. Milestone 3: testing and deployment to production servers."
            ),
            skills=["Python", "FastAPI", "REST API"],
            job_type="fixed", budget=2000.0,
            posted_text="1 hour ago", posted_at="2026-09-24T11:00:00+00:00",
            proposals_text="Less than 5", proposals_min=0, proposals_max=4,
            payment_verified=True, client_rating=4.9, client_spend=5000.0,
            client_spend_text="$5K", client_country="United States",
        )

    def test_scores_apply_and_passes(self):
        s, cfg = scorer()
        ev = s.evaluate(self.job_strong(), NOW)
        assert ev.rule_score >= cfg.scoring.recommend_apply_from
        assert ev.recommendation == "apply"
        assert ev.passed is True
        assert ev.excluded is False


class TestReasonsAndRisksFormatting:
    def test_reasons_never_contain_plus_zero_lines(self):
        s, _ = scorer()
        # Low fixed budget makes the "budget" component compute to 0 -> its reason line must be
        # filtered out entirely rather than shown as "+0 бюджет: ...".
        ev = s.evaluate(job(title="Python dev", job_type="fixed", budget=50.0), NOW)
        assert ev.components["budget"] == 0.0
        assert not any(r.startswith("+0 ") for r in ev.reasons)

    def test_no_reason_starts_with_plus_zero_across_several_jobs(self):
        s, _ = scorer()
        jobs = [
            job(title="dev", job_type="hourly", hourly_min=8.0, hourly_max=10.0),
            job(title="dev"),
            job(title="Need SEO expert", job_type="fixed", budget=500.0),
        ]
        for j in jobs:
            ev = s.evaluate(j, NOW)
            assert not any(r.startswith("+0 ") for r in ev.reasons)

    def test_penalties_appear_in_risks_with_minus_n(self):
        s, _ = scorer()
        ev = s.evaluate(job(title="Need SEO expert", job_type="fixed", budget=500.0), NOW)
        # softened case has a formatted penalty; use the softened marketing/n8n job instead,
        # since the plain SEO title is hard-excluded (no penalty formatting there).
        ev2 = s.evaluate(job(title="Marketing automation with n8n", job_type="fixed", budget=500.0,
                             skills=["n8n"]), NOW)
        assert any("(−25)" in r for r in ev2.risks)


class TestApplyAiBlending:
    def test_blending_formula(self):
        s, cfg = scorer()
        ev = s.evaluate(job(title="Python dev", job_type="fixed", budget=1000.0), NOW)
        rule_score = ev.rule_score
        result = AIResult(relevance=90, relevance_confidence=0.9, recommendation="apply",
                          recommendation_p=0.8, risks=[], explanation="x", model="m")
        outcome = AIOutcome("ok", None, "ok", result)
        apply_ai(ev, outcome, cfg.ai, s)
        w = cfg.ai.score_weight
        expected = max(0, min(100, round((1 - w) * rule_score + w * 90)))
        assert ev.final_score == expected

    def test_override_exclusions_false_keeps_excluded(self):
        s, cfg = scorer()
        cfg.ai.override_exclusions = False
        ev = s.evaluate(job(title="SEO expert", job_type="fixed", budget=500.0), NOW)
        assert ev.excluded is True
        result = AIResult(relevance=100, relevance_confidence=1.0, recommendation="apply",
                          recommendation_p=0.9, risks=[], explanation="x", model="m")
        apply_ai(ev, AIOutcome("ok", None, "ok", result), cfg.ai, s)
        assert ev.excluded is True
        assert ev.recommendation == "skip"

    def test_override_exclusions_true_unexcludes(self):
        s, cfg = scorer()
        cfg.ai.override_exclusions = True
        ev = s.evaluate(job(title="SEO expert", job_type="fixed", budget=500.0), NOW)
        assert ev.excluded is True
        result = AIResult(relevance=100, relevance_confidence=1.0, recommendation="apply",
                          recommendation_p=0.9, risks=[], explanation="x", model="m")
        apply_ai(ev, AIOutcome("ok", None, "ok", result), cfg.ai, s)
        assert ev.excluded is False

    def test_outcome_with_result_none_only_sets_status_and_message(self):
        s, cfg = scorer()
        ev = s.evaluate(job(title="Python dev", job_type="fixed", budget=1000.0), NOW)
        before = ev.final_score
        outcome = AIOutcome("failed", "no answer", "failed_x", None)
        apply_ai(ev, outcome, cfg.ai, s)
        assert ev.ai_status == "failed"
        assert ev.ai_message == "no answer"
        assert ev.final_score == ev.rule_score == before
