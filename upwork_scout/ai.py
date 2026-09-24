"""Optional second stage: job assessment by Jev (TypeSafe) through OpenRouter's System One API.

Jev returns *typed* decisions with probabilities rather than free text, so one request asks:

* ``relevance``      — score on a 5-level scale (0…4) → 0–100;
* ``recommendation`` — choice of apply / review / skip;
* ``risk_<key>``     — one yes/no probability per configured risk question.

The short explanation in the report is composed from those typed answers. Only public job
fields are sent (title, description, skills, budget, client stats shown on the job) — never
cookies, the browser profile or any Upwork account data. Every failure mode (timeout, HTTP
error, 429, invalid JSON, schema mismatch) degrades to "AI-оценка не выполнена" and leaves
the deterministic result untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import AICfg, Secrets
from .fmt import RECOMMENDATION_RU, budget
from .models import Job

log = logging.getLogger(__name__)

RELEVANCE_LEVELS = ["not relevant", "weakly relevant", "partially relevant", "relevant", "highly relevant"]
RECOMMENDATIONS = ("apply", "review", "skip")
# Bump when the questions or the interpretation of answers change: invalidates cached results.
AI_CACHE_VERSION = 1


# --- errors --------------------------------------------------------------------------------
class AIError(Exception):
    code = "failed_api"


class AITimeout(AIError):
    code = "failed_timeout"


class AIRateLimited(AIError):
    code = "failed_rate_limit"


class AIAuthError(AIError):
    code = "failed_auth"


class AIInvalidResponse(AIError):
    code = "failed_invalid"


# --- results -------------------------------------------------------------------------------
@dataclass
class AIResult:
    relevance: int  # 0–100
    relevance_confidence: float | None
    recommendation: str  # apply | review | skip
    recommendation_p: float | None
    risks: list[tuple[str, str, float]]  # (key, label, probability) above threshold
    explanation: str
    model: str | None = None
    cost: float | None = None
    stored_risk_labels: list[str] = field(default_factory=list)  # restored from the cache

    @property
    def risk_labels(self) -> list[str]:
        return [f"{label} ({p:.0%})" for _, label, p in self.risks] + self.stored_risk_labels


@dataclass
class AIOutcome:
    status: Literal["ok", "cached", "skipped", "failed"]
    message: str | None = None
    code: str | None = None
    result: AIResult | None = None


# --- response schema -----------------------------------------------------------------------
def _probability(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError("probability outside [0, 1]")
    return value


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ScoreAnswer(_Answer):
    type: Literal["score"]
    score: float = Field(ge=0, le=len(RELEVANCE_LEVELS) - 1)
    confidence: float | None = Field(None, ge=0, le=1)


class _ChoiceAnswer(_Answer):
    type: Literal["choice"]
    choice: Literal["apply", "review", "skip"]
    probabilities: dict[str, float] = {}
    confidence: float | None = Field(None, ge=0, le=1)

    @field_validator("probabilities")
    @classmethod
    def _check(cls, v: dict[str, float]) -> dict[str, float]:
        for p in v.values():
            _probability(p)
        return v


class _NoulAnswer(_Answer):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


# --- request building ----------------------------------------------------------------------
def build_questions(cfg: AICfg) -> dict[str, dict[str, Any]]:
    focus = cfg.freelancer_focus
    questions: dict[str, dict[str, Any]] = {
        "relevance": {
            "type": "score",
            "instructions": (
                f"How relevant is this Upwork job for a freelance developer focused on: {focus}? "
                "Judge by the actual work required, not by keywords alone."
            ),
            "criteria": list(RELEVANCE_LEVELS),
        },
        "recommendation": {
            "type": "choice",
            "instructions": f"Should a freelance developer focused on {focus} apply to this job?",
            "criteria": {
                "apply": "Good fit: clear and bounded scope, matching stack, fair budget, reliable client",
                "review": "Possibly a fit, but something is unclear and needs a manual check",
                "skip": "Poor fit: wrong skills, unrealistic budget, vague or risky job",
            },
        },
    }
    for key, rq in cfg.risk_questions.items():
        questions[f"risk_{key}"] = {"type": "noul", "instructions": rq.instructions}
    return questions


def build_state(job: Job, max_chars: int) -> dict[str, Any]:
    """Public job characteristics only."""
    na = "not specified"
    description = (job.description or "")[:max_chars]
    if job.payment_verified is None:
        payment = na
    else:
        payment = "verified" if job.payment_verified else "not verified"
    return {
        "job_title": job.title or na,
        "job_description": description or na,
        "skills": job.skills or [],
        "budget": budget(job),
        "experience_level": job.experience_level or na,
        "duration": job.duration or na,
        "proposals": job.proposals_text or na,
        "client_payment": payment,
        "client_rating_out_of_5": job.client_rating if job.client_rating is not None else na,
        "client_total_spent": job.client_spend_text or na,
        "client_country": job.client_country or na,
    }


def ai_cache_key(job: Job, cfg: AICfg, model: str | None) -> str:
    """Fingerprint of everything a Jev answer depends on: the exact state sent, the model,
    the questions, the risk threshold/labels and the interpretation version. Freshness is
    deliberately not part of the state (the rules score it), so ageing does not invalidate."""
    payload = {
        "v": AI_CACHE_VERSION,
        "model": model,
        "state": build_state(job, cfg.description_max_chars),
        "questions": build_questions(cfg),
        "risk_threshold": cfg.risk_threshold,
        "risk_labels": {k: rq.label for k, rq in cfg.risk_questions.items()},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def interpret(answers: Any, cfg: AICfg, model: str | None = None, cost: float | None = None) -> AIResult:
    """Validate Jev answers and turn them into an :class:`AIResult` (raises AIInvalidResponse)."""
    if not isinstance(answers, dict):
        raise AIInvalidResponse("в ответе нет объекта answers")
    try:
        relevance = _ScoreAnswer.model_validate(answers.get("relevance"))
        rec = _ChoiceAnswer.model_validate(answers.get("recommendation"))
        risks: list[tuple[str, str, float]] = []
        for key, rq in cfg.risk_questions.items():
            noul = _NoulAnswer.model_validate(answers.get(f"risk_{key}"))
            if noul.noul >= cfg.risk_threshold:
                risks.append((key, rq.label, noul.noul))
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "answers"
        raise AIInvalidResponse(f"ответ не соответствует схеме ({where}: {first.get('msg', 'invalid')})") from None

    rel = round(relevance.score / (len(RELEVANCE_LEVELS) - 1) * 100)
    rec_p = rec.probabilities.get(rec.choice)
    parts = [f"Jev оценил релевантность в {rel}/100"]
    if relevance.confidence is not None:
        parts[0] += f" (уверенность {relevance.confidence:.0%})"
    rec_text = f"рекомендация — «{RECOMMENDATION_RU[rec.choice]}»"
    if rec_p is not None:
        rec_text += f" с вероятностью {rec_p:.0%}"
    parts.append(rec_text)
    explanation = "; ".join(parts) + "."
    if risks:
        explanation += " Риски: " + "; ".join(f"{label} ({p:.0%})" for _, label, p in risks) + "."
    else:
        explanation += " Существенных рисков Jev не отметил."
    return AIResult(
        relevance=rel,
        relevance_confidence=relevance.confidence,
        recommendation=rec.choice,
        recommendation_p=rec_p,
        risks=risks,
        explanation=explanation,
        model=model,
        cost=cost,
    )


# --- HTTP client ---------------------------------------------------------------------------
def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    err = data.get("error") if isinstance(data, dict) else None
    msg = err.get("message") if isinstance(err, dict) else err
    return " ".join(str(msg or resp.reason_phrase or "").split())[:200]


class JevClient:
    """Minimal System One client with retries on timeouts, 429 and 5xx."""

    def __init__(
        self,
        api_key: str,
        model: str,
        url: str,
        timeout_s: float = 30,
        max_retries: int = 2,
        min_interval_s: float = 0.5,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._key = api_key
        self.model = model
        self.url = url
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.min_interval_s = min_interval_s
        self._http = http or httpx.Client()
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def __repr__(self) -> str:
        return f"JevClient(model={self.model!r}, url={self.url!r})"

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        if self._last_call is not None:
            wait = self.min_interval_s - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def decide(self, state: dict[str, Any], questions: dict[str, Any]) -> tuple[dict[str, Any], str | None, float | None]:
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "X-Title": "upwork-scout"}
        attempt = 0
        while True:
            self._throttle()
            delay: float | None = None
            try:
                resp = self._http.post(self.url, json=body, headers=headers, timeout=self.timeout_s)
            except httpx.TimeoutException:
                error: AIError = AITimeout(f"таймаут запроса к Jev ({self.timeout_s:g} с)")
            except httpx.HTTPError as exc:
                error = AIError(f"сетевая ошибка: {type(exc).__name__}")
            else:
                code = resp.status_code
                if code in (401, 403):
                    raise AIAuthError(f"ключ OpenRouter отклонён (HTTP {code})")
                if code == 402:
                    raise AIAuthError("недостаточно кредитов OpenRouter (HTTP 402)")
                if code == 429:
                    error, delay = AIRateLimited("превышен лимит запросов (HTTP 429)"), _retry_after(resp)
                elif code >= 500:
                    error = AIError(f"ошибка сервера OpenRouter (HTTP {code})")
                elif code >= 400:
                    raise AIError(f"HTTP {code}: {_error_message(resp)}")
                else:
                    try:
                        data = resp.json()
                    except ValueError:
                        raise AIInvalidResponse("ответ не является корректным JSON") from None
                    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                        raise AIInvalidResponse("в ответе нет объекта answers")
                    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                    cost = usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None
                    return data["answers"], data.get("model"), cost
            attempt += 1
            if attempt > self.max_retries:
                raise error
            wait = min(30.0, delay if delay is not None else 1.5 * 2 ** (attempt - 1))
            log.info("Jev: %s — повтор %d/%d через %.1f с", error, attempt, self.max_retries, wait)
            self._sleep(wait)


class AIEvaluator:
    """Runs the AI stage for one run with a per-run budget and a circuit breaker."""

    def __init__(self, cfg: AICfg, secrets: Secrets, client: JevClient | None = None):
        self.cfg = cfg
        self.calls = 0
        self.cost = 0.0
        self.ok = 0
        self.failed = 0
        self._consecutive_failures = 0
        self.stopped_reason: str | None = None
        self.disabled_reason: str | None = None
        self._questions = build_questions(cfg)
        if not cfg.enabled:
            self.disabled_reason = "AI-этап выключен (ai.enabled: false или --no-ai)"
        elif not secrets.openrouter_api_key and client is None:
            self.disabled_reason = "не задан OPENROUTER_API_KEY"
        self.client = client
        if self.disabled_reason is None and client is None:
            self.client = JevClient(
                api_key=secrets.openrouter_api_key or "",
                model=secrets.jev_model,
                url=secrets.jev_api_url,
                timeout_s=cfg.timeout_seconds,
                max_retries=cfg.max_retries,
                min_interval_s=cfg.min_interval_seconds,
            )
        self.model = self.client.model if self.client else secrets.jev_model

    @property
    def active(self) -> bool:
        return self.disabled_reason is None and self.stopped_reason is None

    def evaluate(self, job: Job) -> AIOutcome:
        if self.disabled_reason:
            return AIOutcome("skipped", f"AI-оценка не выполнена: {self.disabled_reason}", "skipped_disabled")
        if self.stopped_reason:
            return AIOutcome("failed", f"AI-оценка не выполнена: {self.stopped_reason}", "failed_stopped")
        if self.calls >= self.cfg.max_jobs_per_run:
            return AIOutcome("skipped", "AI-оценка не выполнена: достигнут лимит ai.max_jobs_per_run", "skipped_limit")
        assert self.client is not None
        self.calls += 1
        try:
            answers, model, cost = self.client.decide(build_state(job, self.cfg.description_max_chars), self._questions)
            result = interpret(answers, self.cfg, model=model, cost=cost)
        except (AIAuthError, AIRateLimited) as exc:
            self.failed += 1
            self.stopped_reason = f"{exc}; AI-этап остановлен до конца запуска"
            log.warning("Jev: %s", self.stopped_reason)
            return AIOutcome("failed", f"AI-оценка не выполнена: {exc}", exc.code)
        except AIError as exc:
            self.failed += 1
            self._consecutive_failures += 1
            log.warning("Jev: вакансия %s — %s", job.job_id, exc)
            if self._consecutive_failures >= self.cfg.max_consecutive_failures:
                self.stopped_reason = f"{self._consecutive_failures} ошибки подряд; AI-этап остановлен до конца запуска"
            return AIOutcome("failed", f"AI-оценка не выполнена: {exc}", exc.code)
        self._consecutive_failures = 0
        self.ok += 1
        self.cost += result.cost or 0.0
        return AIOutcome("ok", None, "ok", result)

    def summary(self) -> str:
        if self.disabled_reason:
            return f"AI-оценка не выполнялась: {self.disabled_reason}."
        text = f"AI-оценка (Jev, {self.model}): успешно {self.ok}, ошибок {self.failed}, запросов {self.calls}"
        if self.cost:
            text += f", стоимость ≈ ${self.cost:.4f}"
        if self.stopped_reason:
            text += f". Остановлено: {self.stopped_reason}"
        return text + "."

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
