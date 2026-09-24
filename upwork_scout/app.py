"""Command-line entry point: one manual run = one pass + one HTML report."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from . import __version__
from .ai import AIEvaluator
from .browser import BrowserLaunchError, open_browser
from .config import Config, ConfigError, Secrets, load_config, load_secrets
from .pipeline import build_context, evaluate_run
from .report import write_report
from .scraper import PassAborted, PassResult, ensure_logged_in, run_pass, short_error
from .storage import Storage

log = logging.getLogger("upwork_scout")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_ABORTED = 0, 1, 2, 3


class _SecretFilter(logging.Filter):
    """Defence in depth: masks secret values should one ever reach a log record."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            message = record.getMessage()
            masked = message
            for secret in self.secrets:
                masked = masked.replace(secret, "***")
            if masked != message:
                record.msg, record.args = masked, None
        return True


def setup_logging(cfg: Config, secrets: Secrets) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    secret_filter = _SecretFilter([secrets.openrouter_api_key or ""])

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.addFilter(lambda r: r.name.startswith("upwork_scout") and r.levelno >= logging.INFO)
    console.addFilter(secret_filter)
    root.addHandler(console)

    log_path = cfg.path(cfg.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setLevel(getattr(logging, cfg.logging.level))
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    file_handler.addFilter(secret_filter)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _notify(message: str) -> None:
    log.info(message)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="upwork_scout",
        description="Отбор вакансий Upwork из ваших сохранённых поисков (только чтение).",
    )
    p.add_argument("--config", help="путь к config.yaml (по умолчанию — в папке проекта)")
    p.add_argument("--show-all", action="store_true", help="показать в отчёте все найденные вакансии, а не только новые")
    p.add_argument("--no-ai", action="store_true", help="не использовать AI-оценку (Jev) в этом запуске")
    p.add_argument("--no-details", action="store_true", help="не открывать страницы вакансий (быстрее)")
    p.add_argument("--load-more", type=int, metavar="N", help="сколько раз нажимать «Load More Jobs» в каждом поиске")
    p.add_argument("--rescore", action="store_true",
                   help="без браузера: пересчитать оценки последнего запуска по текущему config.yaml и создать новый отчёт")
    p.add_argument("--login-only", action="store_true",
                   help="только открыть Upwork в профиле скрипта и дождаться входа (без сбора)")
    p.add_argument("--version", action="version", version=f"upwork-scout {__version__}")
    args = p.parse_args(argv)
    if args.load_more is not None and not 0 <= args.load_more <= 50:
        p.error("--load-more: ожидается число от 0 до 50")
    return args


def apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if args.show_all:
        cfg.report.show = "all"
    if args.no_ai:
        cfg.ai.enabled = False
    if args.no_details:
        cfg.collection.details.mode = "off"
    if args.load_more is not None:
        cfg.collection.load_more_clicks = args.load_more


def _finish(cfg: Config, secrets: Secrets, store: Storage, run_id: int, run_started: datetime,
            status: str, reason: str | None, warnings: list[str], details_summary: str | None,
            update_run: bool = True) -> str:
    """Evaluate the run, write the report (always), record the run outcome."""
    evaluator = AIEvaluator(cfg.ai, secrets)
    try:
        items = evaluate_run(store, cfg, run_id, run_started, evaluator)
        ai_summary = evaluator.summary()
    finally:
        evaluator.close()
    ctx = build_context(store, cfg, run_id, items, run_started, datetime.now().astimezone(),
                        reason, warnings, ai_summary, details_summary)
    path = write_report(ctx, cfg.path(cfg.report.output_dir))
    if update_run:
        store.finish_run(run_id, datetime.now(timezone.utc), status, reason, ctx.searches_found, str(path))
    _notify("")
    _notify(f"Поисков обработано: {ctx.searches_ok} из {ctx.searches_found}; уникальных вакансий: {ctx.unique_jobs}; "
            f"новых: {ctx.new_jobs}; прошли фильтр: {ctx.passed_in_scope}; ошибок поисков: {ctx.search_errors}.")
    _notify(ai_summary)
    if reason:
        _notify(f"Внимание: {reason}")
    _notify(f"Отчёт: {path}")
    return str(path)


def run(cfg: Config, secrets: Secrets, store: Storage) -> int:
    started = datetime.now(timezone.utc)
    run_id = store.start_run(started)
    log.debug("Запуск №%d, %s", run_id, secrets)
    result = PassResult()
    abort: PassAborted | None = None
    failure: str | None = None
    try:
        with open_browser(cfg) as driver:
            run_pass(driver, cfg, store, run_id, started, result, notify=_notify)
    except PassAborted as exc:
        abort = exc
    except BrowserLaunchError as exc:
        failure = str(exc)
    except KeyboardInterrupt:
        abort = PassAborted("interrupted", "Проход прерван пользователем (Ctrl+C). Уже собранные данные сохранены.")
    except Exception as exc:  # the report must still be produced
        log.exception("Непредвиденная ошибка прохода")
        failure = f"Непредвиденная ошибка: {short_error(exc)}. Подробности — в {cfg.logging.file}."
    status = "failed" if failure else ("aborted" if abort else "ok")
    reason = failure or (abort.message if abort else None)
    _finish(cfg, secrets, store, run_id, started, status, reason, result.warnings, result.details_summary())
    if failure:
        return EXIT_FAILED
    return EXIT_ABORTED if abort else EXIT_OK


def rescore(cfg: Config, secrets: Secrets, store: Storage) -> int:
    run_id = store.last_run_with_jobs()
    if run_id is None:
        _notify("В базе ещё нет собранных вакансий — сначала выполните обычный запуск.")
        return EXIT_FAILED
    row = store.get_run(run_id)
    started = datetime.fromisoformat(row["started_at"])
    _notify(f"Пересчёт оценок запуска №{run_id} ({row['started_at']}) по текущему config.yaml.")
    _finish(cfg, secrets, store, run_id, started, row["status"], row["abort_reason"], [], None, update_run=False)
    return EXIT_OK


def login_only(cfg: Config) -> int:
    try:
        with open_browser(cfg) as driver:
            ensure_logged_in(driver, cfg, _notify)
            _notify("Вход выполнен, сессия сохранена в профиле скрипта. Окно закроется через 5 секунд.")
            driver.wait(5)
    except PassAborted as exc:
        _notify(exc.message)
        return EXIT_ABORTED
    except BrowserLaunchError as exc:
        _notify(str(exc))
        return EXIT_FAILED
    except KeyboardInterrupt:
        return EXIT_ABORTED
    return EXIT_OK


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
        apply_overrides(cfg, args)
        secrets = load_secrets(cfg.base_dir)
    except ConfigError as exc:
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(cfg, secrets)
    if args.login_only:
        return login_only(cfg)
    store = Storage(cfg.path(cfg.storage.db_path))
    try:
        return rescore(cfg, secrets, store) if args.rescore else run(cfg, secrets, store)
    finally:
        store.close()
