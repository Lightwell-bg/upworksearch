"""Tests for upwork_scout.ai: the Jev HTTP client, response interpretation and AIEvaluator.

Every HTTP call goes through httpx.MockTransport; nothing here touches the network. All
sleeps are stubbed with a no-op so retry tests run instantly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from upwork_scout.ai import (
    AIAuthError,
    AIError,
    AIEvaluator,
    AIInvalidResponse,
    AIRateLimited,
    JevClient,
    ai_cache_key,
    build_questions,
    build_state,
    interpret,
)
from upwork_scout.config import load_config, Secrets
from upwork_scout.models import Job

CFG = load_config().ai
VALID_ANSWERS = {
    "relevance": {"type": "score", "score": 3.95, "confidence": 0.96},
    "recommendation": {"type": "choice", "choice": "apply",
                       "probabilities": {"apply": 0.52, "review": 0.43, "skip": 0.05}},
    "risk_vague_scope": {"type": "noul", "noul": 0.7},
    "risk_budget_mismatch": {"type": "noul", "noul": 0.1},
    "risk_off_stack": {"type": "noul", "noul": 0.2},
    "risk_red_flags": {"type": "noul", "noul": 0.05},
}


def make_client(handler, **kwargs) -> JevClient:
    kwargs.setdefault("timeout_s", 5)
    kwargs.setdefault("max_retries", 2)
    return JevClient(
        "testkey", "test-model", "https://openrouter.ai/api/v1/systemone",
        http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None, **kwargs,
    )


class TestJevClientSuccess:
    def test_returns_answers_model_cost_and_sends_expected_request(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"answers": VALID_ANSWERS, "model": "test-model",
                                              "usage": {"cost": 0.0002}})

        client = make_client(handler)
        answers, model, cost = client.decide(build_state(Job(job_id="j", url="u", title="t"), 4000),
                                             build_questions(CFG))
        assert answers == VALID_ANSWERS
        assert model == "test-model"
        assert cost == 0.0002
        assert captured["auth"] == "Bearer testkey"
        body = captured["body"]
        assert set(body.keys()) == {"model", "state", "questions"}
        questions = body["questions"]
        assert "relevance" in questions
        assert "recommendation" in questions
        assert any(k.startswith("risk_") for k in questions)


class TestJevClientRetries:
    def test_timeout_twice_then_success(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) <= 2:
                raise httpx.TimeoutException("timeout")
            return httpx.Response(200, json={"answers": VALID_ANSWERS})

        client = make_client(handler, max_retries=2)
        answers, _, _ = client.decide({}, {})
        assert answers == VALID_ANSWERS
        assert len(calls) == 3

    def test_429_always_raises_rate_limited_after_max_retries_plus_one(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, headers={"retry-after": "1.5"}, json={"error": "rate limited"})

        sleeps = []
        client = JevClient(
            "testkey", "test-model", "https://openrouter.ai/api/v1/systemone", max_retries=2,
            min_interval_s=0,  # disable request-pacing sleeps so only retry waits are captured
            http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append,
        )
        with pytest.raises(AIRateLimited):
            client.decide({}, {})
        assert len(calls) == 3  # 1 initial attempt + 2 retries
        # Both retries must wait exactly the server-provided Retry-After, not the default backoff.
        assert sleeps == [1.5, 1.5]

    def test_500_then_success(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(200, json={"answers": VALID_ANSWERS})

        client = make_client(handler, max_retries=2)
        answers, _, _ = client.decide({}, {})
        assert answers == VALID_ANSWERS
        assert len(calls) == 2


class TestJevClientNonRetryableErrors:
    def test_401_raises_auth_error_immediately(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(401, json={"error": "bad key"})

        client = make_client(handler, max_retries=2)
        with pytest.raises(AIAuthError):
            client.decide({}, {})
        assert len(calls) == 1

    def test_402_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"error": "no credits"})

        client = make_client(handler, max_retries=2)
        with pytest.raises(AIAuthError):
            client.decide({}, {})

    def test_400_raises_ai_error_with_no_retry(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400, json={"error": {"message": "bad request"}})

        client = make_client(handler, max_retries=2)
        with pytest.raises(AIError) as exc_info:
            client.decide({}, {})
        assert len(calls) == 1
        assert exc_info.value.code == "failed_api"

    def test_invalid_json_body_raises_invalid_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        client = make_client(handler)
        with pytest.raises(AIInvalidResponse):
            client.decide({}, {})

    def test_missing_answers_raises_invalid_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"model": "m"})

        client = make_client(handler)
        with pytest.raises(AIInvalidResponse):
            client.decide({}, {})


class TestInterpret:
    def test_valid_answers(self):
        result = interpret(VALID_ANSWERS, CFG)
        assert result.relevance == 99
        assert result.recommendation == "apply"
        # Only risk_vague_scope (0.7) is above the configured threshold (0.6).
        assert [key for key, _, _ in result.risks] == ["vague_scope"]

    def test_score_out_of_range_is_invalid(self):
        bad = {**VALID_ANSWERS, "relevance": {"type": "score", "score": 7, "confidence": 0.5}}
        with pytest.raises(AIInvalidResponse):
            interpret(bad, CFG)

    def test_unknown_choice_is_invalid(self):
        bad = {**VALID_ANSWERS, "recommendation": {"type": "choice", "choice": "maybe"}}
        with pytest.raises(AIInvalidResponse):
            interpret(bad, CFG)

    def test_noul_out_of_range_is_invalid(self):
        bad = {**VALID_ANSWERS, "risk_vague_scope": {"type": "noul", "noul": 1.5}}
        with pytest.raises(AIInvalidResponse):
            interpret(bad, CFG)

    def test_missing_risk_key_is_invalid(self):
        bad = dict(VALID_ANSWERS)
        del bad["risk_vague_scope"]
        with pytest.raises(AIInvalidResponse):
            interpret(bad, CFG)


class TestBuildState:
    def test_exact_public_key_set(self):
        job = Job(job_id="j", url="u", title="t", description="d")
        state = build_state(job, 4000)
        # "posted" is deliberately absent: freshness is scored by the rules from posted_at,
        # not sent to Jev, so ageing a job never invalidates its cached AI assessment.
        assert set(state.keys()) == {
            "job_title", "job_description", "skills", "budget", "experience_level",
            "duration", "proposals", "client_payment", "client_rating_out_of_5",
            "client_total_spent", "client_country",
        }

    def test_description_is_truncated(self):
        job = Job(job_id="j", url="u", title="t", description="x" * 5000)
        state = build_state(job, 100)
        assert len(state["job_description"]) == 100


class TestAiCacheKey:
    """ai_cache_key fingerprints everything a cached Jev answer depends on, so config changes
    that would change the actual question/state invalidate stale cache entries."""

    @staticmethod
    def job(**kwargs) -> Job:
        kwargs.setdefault("job_id", "j1")
        kwargs.setdefault("url", "https://www.upwork.com/jobs/~j1")
        kwargs.setdefault("title", "Python dev")
        kwargs.setdefault("description", "some description")
        return Job(**kwargs)

    def test_stable_for_identical_inputs(self):
        j = self.job()
        cfg = load_config().ai
        assert ai_cache_key(j, cfg, "model-a") == ai_cache_key(j, cfg, "model-a")

    def test_changes_with_model(self):
        j = self.job()
        cfg = load_config().ai
        assert ai_cache_key(j, cfg, "model-a") != ai_cache_key(j, cfg, "model-b")

    def test_changes_with_freelancer_focus(self):
        j = self.job()
        base = load_config().ai
        changed = load_config().ai
        changed.freelancer_focus = "Something else entirely"
        assert ai_cache_key(j, base, "model-a") != ai_cache_key(j, changed, "model-a")

    def test_changes_with_risk_threshold(self):
        j = self.job()
        base = load_config().ai
        changed = load_config().ai
        changed.risk_threshold = 0.9
        assert ai_cache_key(j, base, "model-a") != ai_cache_key(j, changed, "model-a")

    def test_changes_with_job_description(self):
        cfg = load_config().ai
        j1 = self.job(description="one description")
        j2 = self.job(description="a totally different description")
        assert ai_cache_key(j1, cfg, "model-a") != ai_cache_key(j2, cfg, "model-a")

    def test_does_not_change_with_posted_text_or_posted_at(self):
        cfg = load_config().ai
        j1 = self.job()
        j2 = self.job(posted_text="2 hours ago", posted_at="2026-09-24T10:00:00+00:00")
        assert ai_cache_key(j1, cfg, "model-a") == ai_cache_key(j2, cfg, "model-a")


class TestAIEvaluator:
    def _job(self, i=1):
        return Job(job_id=f"j{i}", url=f"https://www.upwork.com/jobs/~j{i}", title="Python dev")

    def test_no_key_returns_skipped_and_makes_no_http_call(self):
        secrets = Secrets(openrouter_api_key=None, jev_model="m", jev_api_url="https://x/y")
        cfg = load_config().ai
        evaluator = AIEvaluator(cfg, secrets)
        outcome = evaluator.evaluate(self._job())
        assert outcome.status == "skipped"
        assert evaluator.calls == 0

    def test_circuit_breaker_stops_after_max_consecutive_failures(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500, json={"error": "boom"})

        client = JevClient("k", "m", "https://x/y", max_retries=0,
                           http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
        cfg = load_config().ai
        cfg.max_consecutive_failures = 3
        secrets = Secrets(openrouter_api_key="key", jev_model="m", jev_api_url="https://x/y")
        evaluator = AIEvaluator(cfg, secrets, client=client)

        for i in range(3):
            outcome = evaluator.evaluate(self._job(i))
            assert outcome.status == "failed"
        assert len(calls) == 3

        # further calls return "failed" without another HTTP request
        outcome = evaluator.evaluate(self._job(99))
        assert outcome.status == "failed"
        assert outcome.code == "failed_stopped"
        assert len(calls) == 3

    def test_auth_error_stops_the_stage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad"})

        client = JevClient("k", "m", "https://x/y", max_retries=0,
                           http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
        secrets = Secrets(openrouter_api_key="key", jev_model="m", jev_api_url="https://x/y")
        evaluator = AIEvaluator(load_config().ai, secrets, client=client)

        first = evaluator.evaluate(self._job(1))
        assert first.code == "failed_auth"
        second = evaluator.evaluate(self._job(2))
        assert second.code == "failed_stopped"

    def test_max_jobs_per_run_respected(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"answers": VALID_ANSWERS})

        client = JevClient("k", "m", "https://x/y",
                           http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
        cfg = load_config().ai
        cfg.max_jobs_per_run = 2
        secrets = Secrets(openrouter_api_key="key", jev_model="m", jev_api_url="https://x/y")
        evaluator = AIEvaluator(cfg, secrets, client=client)

        statuses = [evaluator.evaluate(self._job(i)).status for i in range(4)]
        assert statuses == ["ok", "ok", "skipped", "skipped"]
        assert len(calls) == 2

    def test_secrets_repr_never_contains_key(self):
        secrets = Secrets(openrouter_api_key="super-secret-value", jev_model="m", jev_api_url="https://x/y")
        assert "super-secret-value" not in repr(secrets)
