"""Deterministic selection: hard exclusion rules first, then an explainable 0–100 score.

Score = stack (≤40) + freshness (≤10) + competition (≤10) + client (≤20) + budget (≤10)
+ clarity (≤10) − penalties, clamped to 0…100. Every added or removed point produces a
human-readable reason or risk line for the report. A field that is missing gives the
component ``scoring.unknown_share`` of its maximum — "not shown" is not "bad".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .ai import AIOutcome
from .config import AICfg, FiltersCfg, ScoringCfg
from .fmt import LOCATION_RU, age, age_hours, money
from .models import Job
from .textmatch import find_phrases

_LOCATIONS = ("title", "skills", "description")
_BULLET_RE = re.compile(r"^\s*(?:[-*•●▪]|\d+[.)])\s+")


@dataclass
class Evaluation:
    job_id: str
    excluded: bool
    exclusion_reasons: list[str]
    rule_score: int
    final_score: int
    passed: bool
    recommendation: str  # apply | review | skip
    reasons: list[str]
    risks: list[str]
    components: dict[str, float]
    ai_status: str | None = None  # ok | cached | skipped | failed
    ai_code: str | None = None
    ai_message: str | None = None
    ai_relevance: int | None = None
    ai_recommendation: str | None = None
    ai_recommendation_p: float | None = None
    ai_risks: list[str] = field(default_factory=list)
    ai_explanation: str | None = None
    ai_model: str | None = None
    ai_cache_key: str | None = None  # fingerprint of the AI inputs (see ai.ai_cache_key)


def _share_le(value: float, tiers: list[tuple[float, float]]) -> float:
    """Share for the first tier whose limit is ≥ value (tiers: [limit, share])."""
    for limit, share in sorted(tiers):
        if value <= limit:
            return share
    return 0.0


def _share_ge(value: float, tiers: list[tuple[float, float]]) -> float:
    """Share for the highest tier whose limit is ≤ value."""
    for limit, share in sorted(tiers, reverse=True):
        if value >= limit:
            return share
    return 0.0


def _pts(value: float) -> str:
    """Signed whole points for explanations ("+12", "−10")."""
    n = int(abs(value) + 0.5)
    return f"+{n}" if value >= 0 else f"−{n}"


class Scorer:
    def __init__(self, filters: FiltersCfg, scoring: ScoringCfg):
        self.f = filters
        self.s = scoring

    # --- helpers -------------------------------------------------------------------------
    @staticmethod
    def _texts(job: Job) -> dict[str, str]:
        return {"title": job.title or "", "skills": " | ".join(job.skills), "description": job.description or ""}

    def stack_matches(self, job: Job) -> list[tuple[str, str, float]]:
        """(label, location, points) for each matched priority group, best location only."""
        texts = self._texts(job)
        mult = {
            "title": self.s.stack.title_multiplier,
            "skills": self.s.stack.skills_multiplier,
            "description": self.s.stack.description_multiplier,
        }
        out: list[tuple[str, str, float]] = []
        for group in self.s.stack.keywords.values():
            best: tuple[str, str, float] | None = None
            for loc in _LOCATIONS:
                if find_phrases(texts[loc], group.phrases):
                    pts = group.weight * mult[loc]
                    if best is None or pts > best[2]:
                        best = (group.label, loc, pts)
            if best:
                out.append(best)
        return out

    def _stop_hits(self, job: Job) -> dict[str, dict[str, list[str]]]:
        """category key → {location: matched phrases}."""
        texts = self._texts(job)
        hits: dict[str, dict[str, list[str]]] = {}
        for key, cat in self.f.stop_categories.items():
            found = {loc: find_phrases(texts[loc], cat.phrases) for loc in _LOCATIONS}
            found = {loc: ph for loc, ph in found.items() if ph}
            if found:
                hits[key] = found
        return hits

    # --- hard rules ------------------------------------------------------------------------
    def hard_exclusions(self, job: Job) -> list[str]:
        return self._hard_rules(job)[0]

    def _hard_rules(self, job: Job) -> tuple[list[str], set[str]]:
        """(exclusion reasons, keys of stop categories that caused an exclusion)."""
        reasons: list[str] = []
        excluded_categories: set[str] = set()
        if job.job_type == "fixed" and job.budget is not None and job.budget < self.f.min_fixed_budget:
            reasons.append(f"fixed-price бюджет {money(job.budget)} ниже минимума {money(self.f.min_fixed_budget)}")
        if job.job_type == "hourly":
            top = job.hourly_max if job.hourly_max is not None else job.hourly_min
            if top is not None and top < self.f.min_hourly_rate:
                reasons.append(f"почасовая ставка до {money(top)}/ч ниже минимума {money(self.f.min_hourly_rate)}/ч")
        soften = self.f.soften_if_stack_in_title_or_skills and any(
            loc in ("title", "skills") for _, loc, _ in self.stack_matches(job)
        )
        for key, found in self._stop_hits(job).items():
            hard = {loc: ph for loc, ph in found.items() if loc in self.f.exclude_in}
            if hard and not soften:
                loc, phrases = next(iter(hard.items()))
                reasons.append(f"{self.f.stop_categories[key].label}: «{phrases[0]}» {LOCATION_RU[loc]}")
                excluded_categories.add(key)
        return reasons, excluded_categories

    # --- score -----------------------------------------------------------------------------
    def evaluate(self, job: Job, now: datetime) -> Evaluation:
        s, unknown = self.s, self.s.unknown_share
        reasons: list[str] = []
        risks: list[str] = []
        comp: dict[str, float] = {}
        penalties: list[tuple[float, str]] = []
        exclusions, excluded_categories = self._hard_rules(job)

        # Stack
        matches = self.stack_matches(job)
        comp["stack"] = min(sum(p for _, _, p in matches), s.stack.max_points)
        if matches:
            ranked = sorted(matches, key=lambda m: -m[2])
            listed = ", ".join(f"{label} ({LOCATION_RU[loc]})" for label, loc, _ in ranked)
            reasons.append(f"{_pts(comp['stack'])} стек: {listed}")
        else:
            risks.append("нет совпадений с приоритетным стеком")

        # Freshness
        hours = age_hours(job.posted_at, now)
        if hours is None:
            comp["freshness"] = s.freshness.max_points * unknown
            reasons.append(f"{_pts(comp['freshness'])} свежесть: дата публикации не указана (нейтрально)")
        else:
            comp["freshness"] = s.freshness.max_points * _share_le(hours, s.freshness.tiers)
            reasons.append(f"{_pts(comp['freshness'])} свежесть: опубликовано {age(hours)}")

        # Competition
        proposals = job.proposals_max if job.proposals_max is not None else job.proposals_min
        if proposals is None:
            comp["competition"] = s.competition.max_points * unknown
            reasons.append(f"{_pts(comp['competition'])} отклики: количество не указано (нейтрально)")
        else:
            comp["competition"] = s.competition.max_points * _share_le(proposals, s.competition.tiers)
            reasons.append(f"{_pts(comp['competition'])} отклики: {job.proposals_text}")
            if proposals >= 20:
                risks.append(f"высокая конкуренция: {job.proposals_text} откликов")

        # Client
        c = s.client
        client_pts = 0.0
        notes: list[str] = []
        if job.payment_verified is None:
            client_pts += c.payment_verified_points * unknown
            notes.append("оплата: не указано")
        elif job.payment_verified:
            client_pts += c.payment_verified_points
            notes.append("оплата подтверждена")
        else:
            notes.append("оплата не подтверждена")
            penalties.append((s.penalties.payment_unverified, "оплата клиента не подтверждена"))
        if job.client_spend is None:
            client_pts += c.spend_points * unknown
            notes.append("расходы: не указано")
        elif job.client_spend >= c.min_spend:
            client_pts += c.spend_points
            notes.append(f"расходы {job.client_spend_text or money(job.client_spend)}")
        else:
            notes.append(f"расходы {job.client_spend_text or money(job.client_spend)}")
            if job.client_spend == 0:
                risks.append("клиент ещё ничего не тратил на Upwork")
        if job.client_rating is None:
            client_pts += c.rating_points * unknown
            notes.append("рейтинг: нет данных")
        elif job.client_rating >= c.min_rating:
            client_pts += c.rating_points
            notes.append(f"рейтинг {job.client_rating:g}")
        else:
            notes.append(f"рейтинг {job.client_rating:g}")
            if job.client_rating < s.penalties.low_rating_below:
                penalties.append((s.penalties.low_rating_points, f"низкий рейтинг клиента ({job.client_rating:g})"))
        comp["client"] = client_pts
        reasons.append(f"{_pts(client_pts)} клиент: " + ", ".join(notes))

        # Budget
        b = s.budget
        if job.job_type == "fixed" and job.budget is not None:
            comp["budget"] = b.max_points * _share_ge(job.budget, b.fixed_tiers)
            reasons.append(f"{_pts(comp['budget'])} бюджет: fixed {money(job.budget)}")
        elif job.job_type == "hourly" and (job.hourly_max is not None or job.hourly_min is not None):
            top = job.hourly_max if job.hourly_max is not None else job.hourly_min
            comp["budget"] = b.max_points * _share_ge(top or 0, b.hourly_tiers)
            reasons.append(f"{_pts(comp['budget'])} ставка: до {money(top)}/ч")
        else:
            comp["budget"] = b.max_points * unknown
            reasons.append(f"{_pts(comp['budget'])} бюджет: не указан (нейтрально)")

        # Clarity
        cl = s.clarity
        desc = job.description or ""
        if not desc:
            comp["clarity"] = cl.max_points * unknown
            reasons.append(f"{_pts(comp['clarity'])} ясность: описание не загружено (нейтрально)")
        else:
            pts = cl.length_points if cl.min_chars <= len(desc) <= cl.max_chars else 0.0
            scope = find_phrases(desc, cl.scope_phrases)
            pts += cl.phrase_points * min(len(scope), cl.max_phrase_matches)
            lines = [ln for ln in desc.splitlines() if ln.strip()]
            bullets = sum(1 for ln in lines if _BULLET_RE.match(ln))
            if bullets >= 3 or (len(lines) >= 4 and sum(1 for ln in lines if len(ln) < 120) >= 3):
                pts += cl.list_points
            comp["clarity"] = min(pts, cl.max_points)
            detail = f"признаки конкретного объёма: {', '.join(scope[:3])}" if scope else "явных признаков объёма нет"
            reasons.append(f"{_pts(comp['clarity'])} ясность: {detail}")

        # Penalties
        p = s.penalties
        if desc and len(desc) < p.short_description_chars:
            penalties.append((p.short_description_points, "очень короткое описание — задача может быть расплывчатой"))
        vague = find_phrases(desc, p.vague_phrases)
        if vague:
            penalties.append((min(p.vague_max_points, p.vague_phrase_points * len(vague)),
                              "расплывчатые формулировки: " + ", ".join(f"«{v}»" for v in vague[:3])))
        if job.job_type == "fixed" and job.budget is not None and job.budget <= p.big_scope_max_budget:
            big = find_phrases(f"{job.title or ''}\n{desc}", p.big_scope_phrases)
            long_duration = find_phrases(job.duration or "", p.long_duration_markers)
            if big or long_duration:
                why = ", ".join(f"«{x}»" for x in (big + long_duration)[:3])
                penalties.append((p.big_scope_points, f"большой объём при бюджете {money(job.budget)}: {why}"))
        for key, found in self._stop_hits(job).items():
            cat = self.f.stop_categories[key]
            if key in excluded_categories:
                continue
            only_description = set(found) == {"description"}
            amount = cat.penalty * (self.f.description_penalty_share if only_description else 1.0)
            loc, phrases = next(iter(found.items()))
            penalties.append((amount, f"похоже на «{cat.label}»: «{phrases[0]}» {LOCATION_RU[loc]}"))

        for amount, text in penalties:
            risks.append(f"{text} ({_pts(-amount)})" if amount > 0 else text)
        # "Почему подходит" lists only what added points; deductions live in the risks.
        reasons = [r for r in reasons if not r.startswith("+0 ")]
        total = sum(comp.values()) - sum(a for a, _ in penalties)
        rule_score = max(0, min(100, round(total)))

        ev = Evaluation(
            job_id=job.job_id,
            excluded=bool(exclusions),
            exclusion_reasons=exclusions,
            rule_score=rule_score,
            final_score=rule_score,
            passed=False,
            recommendation="skip",
            reasons=reasons,
            risks=list(dict.fromkeys(risks)),
            components={k: round(v, 2) for k, v in comp.items()},
        )
        self.finalize(ev)
        return ev

    def recommendation(self, score: float) -> str:
        if score >= self.s.recommend_apply_from:
            return "apply"
        if score >= self.s.recommend_review_from:
            return "review"
        return "skip"

    def finalize(self, ev: Evaluation) -> None:
        ev.passed = not ev.excluded and ev.final_score >= self.s.min_score
        ev.recommendation = "skip" if ev.excluded else self.recommendation(ev.final_score)


def apply_ai(ev: Evaluation, outcome: AIOutcome, ai_cfg: AICfg, scorer: Scorer) -> None:
    """Blend a Jev result into an evaluation. Hard exclusions are kept unless
    ``ai.override_exclusions`` is explicitly enabled."""
    ev.ai_status, ev.ai_code, ev.ai_message = outcome.status, outcome.code, outcome.message
    r = outcome.result
    if r is None:
        return
    ev.ai_relevance = r.relevance
    ev.ai_recommendation = r.recommendation
    ev.ai_recommendation_p = r.recommendation_p
    ev.ai_risks = r.risk_labels
    ev.ai_explanation = r.explanation
    ev.ai_model = r.model
    w = ai_cfg.score_weight
    ev.final_score = max(0, min(100, round((1 - w) * ev.rule_score + w * r.relevance)))
    if w > 0:
        ev.reasons.append(f"AI (Jev): релевантность {r.relevance}/100 учтена с весом {w:.0%}")
    if (
        ev.excluded
        and ai_cfg.override_exclusions
        and r.recommendation == "apply"
        and (r.recommendation_p or 0) >= 0.7
        and r.relevance >= 75
    ):
        ev.reasons.append("возвращена AI вопреки жёсткому правилу (ai.override_exclusions: true): "
                          + "; ".join(ev.exclusion_reasons))
        ev.excluded = False
    scorer.finalize(ev)
    if r.recommendation == "skip" and (r.recommendation_p or 0) >= 0.6 and ev.recommendation == "apply":
        ev.risks.append(f"AI советует пропустить (вероятность {r.recommendation_p:.0%})")
