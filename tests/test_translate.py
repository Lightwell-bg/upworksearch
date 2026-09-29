"""Tests for upwork_scout.translate: the Russian title/summary translation stage.

Every HTTP call goes through httpx.MockTransport; nothing here touches the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from upwork_scout.config import Secrets, load_config
from upwork_scout.models import Job
from upwork_scout.translate import Translator, cache_key, parse_answer, TranslateError

CFG = load_config().translate


def job(i: int = 1, **kwargs) -> Job:
    kwargs.setdefault("job_id", f"j{i}")
    kwargs.setdefault("url", f"https://www.upwork.com/jobs/~j{i}")
    kwargs.setdefault("title", "Python dev")
    kwargs.setdefault("description", "some description")
    return Job(**kwargs)


def make_translator(handler, cfg=None, secrets=None, sleep=lambda s: None) -> Translator:
    cfg = cfg or load_config().translate
    secrets = secrets or Secrets(openrouter_api_key="testkey", jev_model="m", jev_api_url="https://x/y")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Translator(cfg, secrets, http=http, sleep=sleep)


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": '{"title_ru": "Заголовок", "summary_ru": "Резюме перевода."}'}}],
        "usage": {"cost": 0.0001},
    })


class TestCacheKey:
    def test_stable_for_identical_inputs(self):
        j = job()
        assert cache_key(j, CFG) == cache_key(j, CFG)

    def test_changes_with_model(self):
        other = load_config().translate
        other.model = "some-other-model"
        assert cache_key(job(), CFG) != cache_key(job(), other)

    def test_changes_with_title(self):
        assert cache_key(job(title="A"), CFG) != cache_key(job(title="B"), CFG)

    def test_changes_with_description(self):
        assert cache_key(job(description="one"), CFG) != cache_key(job(description="two"), CFG)

    def test_stable_when_unrelated_fields_change(self):
        j1 = job(posted_text="2 hours ago")
        j2 = job(posted_text="yesterday", client_country="Canada")
        assert cache_key(j1, CFG) == cache_key(j2, CFG)


class TestParseAnswer:
    def test_valid_json(self):
        result = parse_answer('{"title_ru": "Заголовок", "summary_ru": "Резюме"}')
        assert result.title_ru == "Заголовок"
        assert result.summary_ru == "Резюме"

    def test_fenced_json_block(self):
        result = parse_answer('```json\n{"title_ru": "Заголовок", "summary_ru": "Резюме"}\n```')
        assert result.title_ru == "Заголовок"
        assert result.summary_ru == "Резюме"

    def test_non_string_content_raises(self):
        with pytest.raises(TranslateError):
            parse_answer(123)

    def test_non_json_raises(self):
        with pytest.raises(TranslateError):
            parse_answer("not json at all")

    def test_non_object_json_raises(self):
        with pytest.raises(TranslateError):
            parse_answer("[1, 2, 3]")

    def test_empty_title_raises(self):
        with pytest.raises(TranslateError):
            parse_answer('{"title_ru": "", "summary_ru": "x"}')

    def test_missing_title_raises(self):
        with pytest.raises(TranslateError):
            parse_answer('{"summary_ru": "x"}')

    def test_missing_summary_raises(self):
        with pytest.raises(TranslateError):
            parse_answer('{"title_ru": "x"}')

    def test_truncates_to_max_lengths(self):
        long_title = "x" * 500
        long_summary = "y" * 2000
        result = parse_answer(json.dumps({"title_ru": long_title, "summary_ru": long_summary}))
        assert len(result.title_ru) == 300
        assert len(result.summary_ru) == 1500


class TestTranslatorSuccess:
    def test_returns_translation_and_sends_expected_request(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return ok_handler(request)

        t = make_translator(handler)
        result = t.translate(job())
        assert result.title_ru == "Заголовок"
        assert result.summary_ru == "Резюме перевода."
        assert captured["auth"] == "Bearer testkey"
        body = captured["body"]
        assert body["model"] == CFG.model
        assert body["response_format"] == {"type": "json_object"}
        messages = body["messages"]
        assert [m["role"] for m in messages] == ["system", "user"]
        assert "Python dev" in messages[1]["content"]
        assert "some description" in messages[1]["content"]

    def test_cost_accumulated_from_usage(self):
        t = make_translator(ok_handler)
        t.translate(job())
        assert t.cost == pytest.approx(0.0001)
        assert t.done == 1


class TestTranslatorDisabled:
    def test_disabled_when_cfg_enabled_false_makes_no_http_call(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return ok_handler(request)

        cfg = load_config().translate
        cfg.enabled = False
        t = make_translator(handler, cfg=cfg)
        assert t.translate(job()) is None
        assert calls == []

    def test_disabled_when_no_key_makes_no_http_call(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return ok_handler(request)

        t = Translator(load_config().translate,
                       Secrets(openrouter_api_key=None, jev_model="m", jev_api_url="https://x/y"),
                       http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
        assert t.translate(job()) is None
        assert calls == []
        assert t.disabled_reason is not None


class TestTranslatorStopsTheStage:
    def test_401_stops_the_stage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        t = make_translator(handler)
        assert t.translate(job(1)) is None
        assert t.stopped_reason is not None

        calls = []

        def handler2(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={})

        t._http = httpx.Client(transport=httpx.MockTransport(handler2))
        assert t.translate(job(2)) is None
        assert calls == []  # the stage is stopped: no further HTTP call at all

    def test_402_stops_the_stage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"error": "no credits"})

        t = make_translator(handler)
        assert t.translate(job()) is None
        assert t.stopped_reason is not None

    def test_429_exhausted_stops_the_stage(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429, json={"error": "rate limited"})

        cfg = load_config().translate
        cfg.max_retries = 2
        t = make_translator(handler, cfg=cfg)
        assert t.translate(job()) is None
        assert len(calls) == 3  # 1 initial attempt + 2 retries
        assert t.stopped_reason is not None


class TestTranslatorRetries:
    def test_5xx_then_success(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, json={"error": "unavailable"})
            return ok_handler(request)

        t = make_translator(handler)
        result = t.translate(job())
        assert result is not None
        assert len(calls) == 2

    def test_timeout_retries(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.TimeoutException("timeout")
            return ok_handler(request)

        t = make_translator(handler)
        result = t.translate(job())
        assert result is not None
        assert len(calls) == 2


class TestTranslatorCatchesUnexpectedExceptions:
    """Anything raised from the request (not just TranslateError) must degrade gracefully to
    "no translation" for that job and stop the stage, rather than crashing the whole run."""

    def test_unexpected_exception_from_handler_returns_none_and_stops_the_stage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("boom from mock handler")

        t = make_translator(handler)
        assert t.translate(job(1)) is None
        assert t.failed == 1
        assert t.stopped_reason is not None

        calls = []

        def handler2(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return ok_handler(request)

        t._http = httpx.Client(transport=httpx.MockTransport(handler2))
        assert t.translate(job(2)) is None
        assert calls == []  # already stopped: no further HTTP call

    def test_httpx_invalid_url_style_exception_stops_the_stage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.InvalidURL("bad url")

        t = make_translator(handler)
        assert t.translate(job()) is None
        assert t.stopped_reason is not None


class TestTranslatorCircuitBreaker:
    def test_stops_after_max_consecutive_failures(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        cfg = load_config().translate
        cfg.max_retries = 0
        cfg.max_consecutive_failures = 2
        t = make_translator(handler, cfg=cfg)

        assert t.translate(job(0)) is None
        assert t.stopped_reason is None
        assert t.translate(job(1)) is None
        assert t.stopped_reason is not None
        stopped_after_two = t.stopped_reason

        assert t.translate(job(2)) is None
        assert t.stopped_reason == stopped_after_two  # unchanged: already stopped


class TestTranslatorMaxJobsPerRun:
    def test_limit_respected(self):
        cfg = load_config().translate
        cfg.max_jobs_per_run = 1
        t = make_translator(ok_handler, cfg=cfg)
        assert t.translate(job(1)) is not None
        assert t.translate(job(2)) is None
        assert t.calls == 1


class TestTranslatorSummary:
    def test_summary_text_with_results(self):
        t = make_translator(ok_handler)
        t.translate(job())
        text = t.summary()
        assert "переведено 1" in text
        assert CFG.model in text

    def test_summary_text_when_disabled(self):
        cfg = load_config().translate
        cfg.enabled = False
        t = make_translator(ok_handler, cfg=cfg)
        assert "не выполнялся" in t.summary()
