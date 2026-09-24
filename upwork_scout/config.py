"""Configuration loading: non-secret settings from config.yaml, secrets from .env.

Every section has defaults so a partially filled config.yaml still works; the shipped
config.yaml is the documented source of truth for weights, thresholds and phrases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(Exception):
    """Raised when config.yaml is missing or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserCfg(_Section):
    profile_dir: str = "browser-profile"
    channel: str = "chrome"
    login_timeout_minutes: float = Field(10, gt=0)
    navigation_timeout_seconds: float = Field(45, gt=0)
    discovery_wait_seconds: float = Field(25, gt=0)
    cards_wait_seconds: float = Field(20, gt=0)
    delay_between_pages_seconds: tuple[float, float] = (3.0, 6.0)

    @field_validator("delay_between_pages_seconds")
    @classmethod
    def _ordered(cls, v: tuple[float, float]) -> tuple[float, float]:
        lo, hi = v
        if lo < 0 or hi < lo:
            raise ValueError("ожидается [min, max], 0 ≤ min ≤ max")
        return v


class UpworkCfg(_Section):
    find_work_url: str = "https://www.upwork.com/nx/find-work/"

    @field_validator("find_work_url")
    @classmethod
    def _upwork_only(cls, v: str) -> str:
        p = urlparse(v)
        if p.scheme != "https" or p.hostname != "www.upwork.com" or not p.path.startswith("/nx/find-work"):
            raise ValueError("должен быть https://www.upwork.com/nx/find-work/…")
        return v


class DetailsCfg(_Section):
    mode: Literal["off", "missing", "always"] = "missing"
    only_new: bool = True
    max_per_run: int = Field(40, ge=0)
    max_attempts: int = Field(3, ge=1, le=10)


class CollectionCfg(_Section):
    load_more_clicks: int = Field(0, ge=0, le=50)
    load_more_wait_seconds: float = Field(15, gt=0)
    details: DetailsCfg = DetailsCfg()
    debug_dump_html: bool = False


class StorageCfg(_Section):
    db_path: str = "data/upwork.sqlite3"


class ReportCfg(_Section):
    output_dir: str = "outputs"
    show: Literal["new", "all"] = "new"
    show_below_threshold: bool = True
    show_excluded: bool = True


class LoggingCfg(_Section):
    file: str = "logs/upwork-scout.log"
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class StopCategory(_Section):
    label: str
    penalty: float = Field(25, ge=0)
    phrases: list[str] = []


class FiltersCfg(_Section):
    min_fixed_budget: float = Field(100, ge=0)
    min_hourly_rate: float = Field(15, ge=0)
    exclude_in: list[Literal["title", "skills", "description"]] = ["title", "skills"]
    soften_if_stack_in_title_or_skills: bool = True
    description_penalty_share: float = Field(0.5, ge=0, le=1)
    stop_categories: dict[str, StopCategory] = {}


class KeywordGroup(_Section):
    label: str
    weight: float = Field(ge=0)
    phrases: list[str]


class StackCfg(_Section):
    max_points: float = Field(40, ge=0)
    title_multiplier: float = 1.5
    skills_multiplier: float = 1.25
    description_multiplier: float = 1.0
    keywords: dict[str, KeywordGroup] = {}


class TieredCfg(_Section):
    max_points: float = Field(10, ge=0)
    tiers: list[tuple[float, float]] = []


class ClientCfg(_Section):
    payment_verified_points: float = 6
    spend_points: float = 7
    min_spend: float = 1000
    rating_points: float = 7
    min_rating: float = 4.5


class BudgetCfg(_Section):
    max_points: float = 10
    fixed_tiers: list[tuple[float, float]] = []
    hourly_tiers: list[tuple[float, float]] = []


class ClarityCfg(_Section):
    max_points: float = 10
    min_chars: int = 300
    max_chars: int = 5000
    length_points: float = 4
    phrase_points: float = 2
    max_phrase_matches: int = 2
    list_points: float = 2
    scope_phrases: list[str] = []


class PenaltiesCfg(_Section):
    payment_unverified: float = 10
    low_rating_below: float = 4.0
    low_rating_points: float = 10
    short_description_chars: int = 150
    short_description_points: float = 15
    vague_phrase_points: float = 5
    vague_max_points: float = 15
    vague_phrases: list[str] = []
    big_scope_max_budget: float = 500
    big_scope_points: float = 25
    big_scope_phrases: list[str] = []
    long_duration_markers: list[str] = []


class ScoringCfg(_Section):
    min_score: float = Field(40, ge=0, le=100)
    recommend_apply_from: float = Field(70, ge=0, le=100)
    recommend_review_from: float = Field(45, ge=0, le=100)
    unknown_share: float = Field(0.5, ge=0, le=1)
    stack: StackCfg = StackCfg()
    freshness: TieredCfg = TieredCfg()
    competition: TieredCfg = TieredCfg()
    client: ClientCfg = ClientCfg()
    budget: BudgetCfg = BudgetCfg()
    clarity: ClarityCfg = ClarityCfg()
    penalties: PenaltiesCfg = PenaltiesCfg()


class RiskQuestion(_Section):
    label: str
    instructions: str


class AICfg(_Section):
    enabled: bool = True
    max_jobs_per_run: int = Field(60, ge=0)
    min_rule_score: float = Field(25, ge=0, le=100)
    score_weight: float = Field(0.35, ge=0, le=1)
    timeout_seconds: float = Field(30, gt=0)
    max_retries: int = Field(2, ge=0, le=10)
    min_interval_seconds: float = Field(0.5, ge=0)
    max_consecutive_failures: int = Field(3, ge=1)
    description_max_chars: int = Field(4000, ge=200)
    override_exclusions: bool = False
    freelancer_focus: str = "Python, automation, API integrations, AI/LLM agents"
    risk_threshold: float = Field(0.6, ge=0, le=1)
    risk_questions: dict[str, RiskQuestion] = {}


class Config(_Section):
    browser: BrowserCfg = BrowserCfg()
    upwork: UpworkCfg = UpworkCfg()
    collection: CollectionCfg = CollectionCfg()
    storage: StorageCfg = StorageCfg()
    report: ReportCfg = ReportCfg()
    logging: LoggingCfg = LoggingCfg()
    filters: FiltersCfg = FiltersCfg()
    scoring: ScoringCfg = ScoringCfg()
    ai: AICfg = AICfg()

    # Directory that relative paths in the config are resolved against (not read from YAML).
    base_dir: Path = Field(default=PROJECT_ROOT, exclude=True)

    def path(self, relative: str) -> Path:
        """Resolve a config path relative to the project directory."""
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p)


def load_config(path: str | Path | None = None) -> Config:
    """Read and validate config.yaml. Relative paths are resolved against its folder."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"Не найден файл настроек: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка синтаксиса YAML в {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: ожидается словарь настроек верхнего уровня")
    try:
        cfg = Config.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError: keep the message readable
        raise ConfigError(f"Неверные значения в {cfg_path}:\n{exc}") from exc
    cfg.base_dir = cfg_path.resolve().parent
    return cfg


@dataclass(frozen=True)
class Secrets:
    """Values from .env (or the process environment). Never log these."""

    openrouter_api_key: str | None
    jev_model: str
    jev_api_url: str

    def __repr__(self) -> str:  # never leak the key through logging/tracebacks
        key = "set" if self.openrouter_api_key else "unset"
        return f"Secrets(openrouter_api_key=<{key}>, jev_model={self.jev_model!r}, jev_api_url={self.jev_api_url!r})"


DEFAULT_JEV_MODEL = "~typesafe/jev-latest"
DEFAULT_JEV_URL = "https://openrouter.ai/api/v1/systemone"


def load_secrets(base_dir: Path = PROJECT_ROOT) -> Secrets:
    """Non-empty values in .env win; empty ones fall back to the process environment."""
    file_values = dotenv_values(base_dir / ".env") if (base_dir / ".env").is_file() else {}

    def pick(name: str) -> str | None:
        value = (file_values.get(name) or "").strip()
        if value:
            return value
        env = (os.environ.get(name) or "").strip()
        return env or None

    url = pick("JEV_API_URL") or DEFAULT_JEV_URL
    if urlparse(url).scheme != "https":
        raise ConfigError("JEV_API_URL должен начинаться с https://")
    return Secrets(
        openrouter_api_key=pick("OPENROUTER_API_KEY"),
        jev_model=pick("JEV_MODEL") or DEFAULT_JEV_MODEL,
        jev_api_url=url,
    )


def default_chrome_user_data_dir() -> Path | None:
    """The user's everyday Chrome profile root on Windows (must never be used by the scraper)."""
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "Google" / "Chrome" / "User Data" if local else None


def validate_profile_dir(profile: Path) -> None:
    """Refuse to automate the user's everyday Chrome profile."""
    daily = default_chrome_user_data_dir()
    if daily is None:
        return
    try:
        resolved, daily_resolved = profile.resolve(), daily.resolve()
    except OSError:
        return
    if resolved == daily_resolved or daily_resolved in resolved.parents:
        raise ConfigError(
            "browser.profile_dir указывает на повседневный профиль Chrome. "
            "Укажите отдельную папку (по умолчанию browser-profile)."
        )
