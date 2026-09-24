"""One read-only pass over all saved searches, sequentially, in a single tab.

Flow: wait for the user to be logged in → discover saved searches on Find Work → open each
search, collect the loaded cards (optionally pressing "Load More Jobs") → optionally open
job pages for missing fields. Data are committed after every search.

Stopping rules:
* CAPTCHA / security check or a login page *after the pass started* → the whole pass stops
  (:class:`PassAborted`); collected data stay in the database and the report is still built;
* any other error in one search → recorded for that search, the pass continues.

Before the pass starts (the login phase) the script only waits and watches: the user is in
control of the window and may log in or complete Upwork's checks manually.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .browser import BrowserClosedError
from .config import Config
from .discovery import parse_saved_searches
from .guards import PageState, detect_page_state
from .htmltext import inline_text, sanitize_for_debug, soup_of
from .ids import is_find_work_url, is_upwork_url, normalize_job_id
from .models import Job, SavedSearch
from .parsing import parse_job_details, parse_search_page
from .scoring import Scorer
from .storage import Storage

log = logging.getLogger(__name__)
Notify = Callable[[str], None]

SECURITY_MESSAGE = (
    "Upwork показал CAPTCHA или проверку безопасности. Автоматический проход остановлен — "
    "скрипт не обходит такие проверки. Подождите, откройте Upwork в профиле скрипта "
    "(run.bat --login-only), пройдите проверку сами и запустите отбор позже."
)
AUTH_MESSAGE = (
    "Сессия Upwork потеряна (открылась страница входа). Сбор остановлен, уже собранные данные "
    "сохранены. Запустите скрипт снова и войдите в открывшемся окне Chrome."
)
BROWSER_CLOSED_MESSAGE = "Окно Chrome было закрыто во время прохода. Уже собранные данные сохранены."
DISCOVERY_MESSAGE = (
    "На странице Find Work не найдено ни одной ссылки на сохранённый поиск (/nx/find-work/<число>). "
    "Возможно, у аккаунта нет сохранённых поисков или изменился интерфейс Upwork. Проход завершён: "
    "список поисков не подставляется вручную."
)
DETAIL_MARKERS = ("About the client", "Activity on this job", "Proposals", "Job details")


class Driver(Protocol):
    def goto(self, url: str) -> None: ...
    def current_url(self) -> str: ...
    def html(self) -> str: ...
    def count_job_links(self) -> int: ...
    def count_search_links(self) -> int: ...
    def body_contains_any(self, needles: Sequence[str], timeout_s: float) -> bool: ...
    def wait(self, seconds: float) -> None: ...
    def pause_between_pages(self) -> None: ...
    def scroll_through(self, max_steps: int = 8) -> None: ...
    def click_load_more(self, wait_s: float) -> bool: ...


class PassAborted(Exception):
    """The whole pass must stop (security check, lost session, closed window, …)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class PassResult:
    searches: list[SavedSearch] = field(default_factory=list)
    started: bool = False  # login confirmed, automatic pass began
    details_attempted: int = 0
    details_fetched: int = 0
    details_failed: int = 0
    warnings: list[str] = field(default_factory=list)

    def details_summary(self) -> str | None:
        if not self.details_attempted:
            return None
        text = f"Углублённый сбор: открыто страниц вакансий {self.details_fetched} из {self.details_attempted}"
        if self.details_failed:
            text += f", ошибок {self.details_failed}"
        return text + "."


def short_error(exc: BaseException) -> str:
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first}"[:240] if first else type(exc).__name__


def check_page(driver: Driver, html: str | None = None) -> None:
    """Raise :class:`PassAborted` on a security check or a login page."""
    state = detect_page_state(driver.current_url(), driver.html() if html is None else html)
    if state is PageState.SECURITY_CHECK:
        raise PassAborted("security", SECURITY_MESSAGE)
    if state is PageState.LOGIN:
        raise PassAborted("auth", AUTH_MESSAGE)


def raise_if_blocked(driver: Driver) -> None:
    """After a failed browser action: a redirect to a challenge or login page must still stop
    the whole pass instead of being recorded as an ordinary per-search error."""
    try:
        check_page(driver)
    except (PassAborted, BrowserClosedError):
        raise
    except Exception:  # the page cannot even be read: keep the original error
        log.debug("Состояние страницы после ошибки определить не удалось", exc_info=True)


def ensure_logged_in(driver: Driver, cfg: Config, notify: Notify,
                     clock: Callable[[], float] = time.monotonic, poll_s: float = 2.0) -> None:
    """Open Find Work and wait (passively) until the user is logged in."""
    url = cfg.upwork.find_work_url
    driver.goto(url)
    timeout_s = cfg.browser.login_timeout_minutes * 60
    deadline = clock() + timeout_s
    said_login = said_security = False
    elsewhere_since: float | None = None
    while True:
        current = driver.current_url()
        state = detect_page_state(current, driver.html())
        if state is PageState.OK and is_find_work_url(current):
            if said_login or said_security:
                notify("Вход подтверждён, начинаю проход.")
            return
        if state is PageState.LOGIN:
            elsewhere_since = None
            if not said_login:
                notify(f"Войдите в Upwork в открытом окне Chrome. Скрипт ждёт до "
                       f"{cfg.browser.login_timeout_minutes:g} мин и продолжит сам.")
                said_login = True
        elif state is PageState.SECURITY_CHECK:
            elsewhere_since = None
            if not said_security:
                notify("Upwork показывает проверку безопасности. Скрипт её не проходит и ничего не нажимает. "
                       "Если хотите продолжить, пройдите её сами в окне Chrome; иначе закройте окно.")
                said_security = True
        elif is_upwork_url(current):  # an ordinary Upwork page other than Find Work (e.g. after login)
            if elsewhere_since is None:
                elsewhere_since = clock()
            elif clock() - elsewhere_since >= 10:
                driver.goto(url)
                elsewhere_since = None
        else:  # another site, e.g. Google/Apple sign-in: never interrupt the user
            elsewhere_since = None
        if clock() >= deadline:
            raise PassAborted("login_timeout",
                              f"Вход в Upwork не выполнен за {cfg.browser.login_timeout_minutes:g} мин. "
                              "Запустите скрипт снова и войдите в открывшемся окне.")
        driver.wait(poll_s)


DISCOVERY_MIN_WAIT_S = 4.0  # never stop earlier than this after the first saved-search link
DISCOVERY_QUIET_S = 3.0  # the set of search IDs must stay unchanged this long


def discover_searches(driver: Driver, cfg: Config, clock: Callable[[], float] = time.monotonic,
                      warnings: list[str] | None = None) -> list[SavedSearch]:
    """Saved searches from the Find Work page.

    The tab list is rendered by a SPA and may grow after the first paint, so discovery waits
    until the *set* of search IDs has been stable for a quiet period (and at least a minimum
    time after the first link appeared), bounded by ``browser.discovery_wait_seconds``."""
    deadline = clock() + cfg.browser.discovery_wait_seconds
    first_seen: float | None = None
    stable_since = clock()
    previous: tuple[str, ...] | None = None
    searches: list[SavedSearch] = []
    while True:
        html = driver.html()
        check_page(driver, html)
        searches = parse_saved_searches(html)
        ids = tuple(s.search_id for s in searches)
        now = clock()
        if ids != previous:
            previous, stable_since = ids, now
        if ids and first_seen is None:
            first_seen = now
        if ids and now - first_seen >= DISCOVERY_MIN_WAIT_S and now - stable_since >= DISCOVERY_QUIET_S:
            break
        if now >= deadline:
            if ids and now - stable_since < DISCOVERY_QUIET_S and warnings is not None:
                warnings.append("Список сохранённых поисков ещё менялся к концу ожидания "
                                f"({cfg.browser.discovery_wait_seconds:g} с) — часть поисков могла не попасть в проход. "
                                "Увеличьте browser.discovery_wait_seconds.")
            break
        driver.wait(1.0)
    if not searches:
        raise PassAborted("discovery", DISCOVERY_MESSAGE)
    return searches


def _wait_for_cards(driver: Driver, timeout_s: float, clock: Callable[[], float]) -> int:
    deadline = clock() + timeout_s
    previous, polls = -1, 0
    while True:
        count = driver.count_job_links()
        if count > 0 and count == previous:
            return count
        previous = count
        polls += 1
        if polls % 4 == 0:
            check_page(driver)
        if clock() >= deadline:
            return count
        driver.wait(0.75)


def collect_search(driver: Driver, cfg: Config, search: SavedSearch, now: datetime,
                   clock: Callable[[], float] = time.monotonic) -> tuple[list[Job], str]:
    """Open one saved search and parse its cards; returns (jobs, page_html)."""
    driver.goto(search.url)
    check_page(driver)
    if _wait_for_cards(driver, cfg.browser.cards_wait_seconds, clock) > 0:
        driver.scroll_through()
    for _ in range(cfg.collection.load_more_clicks):
        if not driver.click_load_more(cfg.collection.load_more_wait_seconds):
            break
        check_page(driver)
    html = driver.html()
    check_page(driver)
    return parse_search_page(html, now), html


def _dump_html(cfg: Config, search: SavedSearch, html: str, now: datetime) -> None:
    """Save a *sanitised* copy of a search page without cards (see sanitize_for_debug)."""
    folder = cfg.path("debug")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"search-{search.search_id}-{now:%Y%m%d-%H%M%S}.html"
    path.write_text(sanitize_for_debug(html), encoding="utf-8")
    log.info("Очищенный HTML страницы поиска без карточек сохранён: %s", path)


class DetailsRejected(Exception):
    """An opened page is not a usable page of the requested job; nothing is stored."""


_UNAVAILABLE_MARKERS = (
    "no longer available",
    "job is private",
    "has been removed",
    "page not found",
    "job not found",
    "doesn't exist",
    "does not exist",
)


def validated_details(job: Job, final_url: str, html: str, ready: bool, now: datetime) -> Job:
    """Parse a job page only if it really is the requested job's page.

    Rejected: page never showed job markers; redirect to a non-job page or to *any* other job
    ID (a matching title is not proof of identity — duplicate titles are common); "no longer
    available" pages; pages without minimal job structure (title + description or type)."""
    if not ready:
        raise DetailsRejected("страница вакансии не загрузилась")
    details = parse_job_details(html, job.job_id, now)
    final_id = normalize_job_id(final_url)
    if final_id is None:
        raise DetailsRejected("открылась не страница вакансии")
    if final_id != job.job_id:
        raise DetailsRejected("открылась страница другой вакансии")
    soup = soup_of(html)
    heading = " ".join(inline_text(el) for el in soup.find_all(["title", "h1", "h2"])[:4]).lower()
    if any(marker in heading for marker in _UNAVAILABLE_MARKERS):
        raise DetailsRejected("вакансия недоступна или удалена")
    if not details.title or not (details.description or details.job_type):
        raise DetailsRejected("на странице нет данных вакансии")
    return details


def run_pass(driver: Driver, cfg: Config, store: Storage, run_id: int, now: datetime,
             result: PassResult, notify: Notify, clock: Callable[[], float] = time.monotonic) -> None:
    try:
        _run_pass(driver, cfg, store, run_id, now, result, notify, clock)
    except BrowserClosedError:
        raise PassAborted("browser_closed", BROWSER_CLOSED_MESSAGE) from None


def _run_pass(driver: Driver, cfg: Config, store: Storage, run_id: int, now: datetime,
              result: PassResult, notify: Notify, clock: Callable[[], float]) -> None:
    ensure_logged_in(driver, cfg, notify, clock)
    result.started = True
    searches = discover_searches(driver, cfg, clock, result.warnings)
    result.searches = searches
    store.upsert_searches(searches, now)
    for pos, s in enumerate(searches):  # listed up front so an aborted pass shows what was skipped
        store.record_search_result(run_id, s.search_id, pos, "skipped", 0, "проход остановлен до этого поиска")
    notify(f"Найдено сохранённых поисков: {len(searches)} — " + ", ".join(f"«{s.name}»" for s in searches))

    for pos, s in enumerate(searches):
        notify(f"[{pos + 1}/{len(searches)}] {s.name}")
        try:
            jobs, html = collect_search(driver, cfg, s, now, clock)
        except PassAborted:
            store.record_search_result(run_id, s.search_id, pos, "aborted", 0, "проход остановлен на этом поиске")
            raise
        except BrowserClosedError:
            store.record_search_result(run_id, s.search_id, pos, "aborted", 0, "окно Chrome закрыто")
            raise
        except Exception as exc:  # isolate one search…
            try:  # …unless the failure was a redirect to a challenge/login page
                raise_if_blocked(driver)
            except (PassAborted, BrowserClosedError):
                store.record_search_result(run_id, s.search_id, pos, "aborted", 0, "проход остановлен на этом поиске")
                raise
            message = short_error(exc)
            log.warning("Поиск «%s»: %s", s.name, message, exc_info=True)
            store.record_search_result(run_id, s.search_id, pos, "error", 0, message)
            notify(f"    ошибка: {message}")
        else:
            store.record_jobs(run_id, s, jobs, now)
            note = None if jobs else "карточки не найдены: поиск пуст или изменилась разметка Upwork"
            store.record_search_result(run_id, s.search_id, pos, "ok", len(jobs), note)
            notify(f"    карточек: {len(jobs)}")
            if not jobs and cfg.collection.debug_dump_html:
                _dump_html(cfg, s, html, now)
        if pos + 1 < len(searches):
            driver.pause_between_pages()

    rows = store.search_runs(run_id)
    ok_rows = [r for r in rows if r.status == "ok"]
    if ok_rows and all(r.cards_found == 0 for r in ok_rows):
        result.warnings.append(
            "Ни в одном поиске не найдено карточек. Если на сайте вакансии есть, вероятно, изменилась разметка "
            "Upwork: включите collection.debug_dump_html в config.yaml и проверьте файлы в папке debug/."
        )
    fetch_details(driver, cfg, store, run_id, now, result, notify)


def fetch_details(driver: Driver, cfg: Config, store: Storage, run_id: int, now: datetime,
                  result: PassResult, notify: Notify) -> None:
    """Open job pages (same tab, one by one) for jobs whose cards lack key fields."""
    dcfg = cfg.collection.details
    if dcfg.mode == "off" or dcfg.max_per_run == 0:
        return
    scorer = Scorer(cfg.filters, cfg.scoring)
    carried: list[Job] = []  # failed earlier or deferred by the limit: go first
    fresh: list[Job] = []
    for job_id in store.run_job_ids(run_id):
        rec = store.get_record(job_id)
        if rec is None or rec.details_fetched_at or rec.details_attempts >= dcfg.max_attempts:
            continue
        is_carried = rec.details_attempts > 0 or rec.details_pending
        if dcfg.only_new and rec.first_seen_run_id != run_id and not is_carried:
            continue
        if dcfg.mode == "missing" and not rec.job.missing_fields():
            continue
        if scorer.hard_exclusions(rec.job):
            continue
        (carried if is_carried else fresh).append(rec.job)
    ordered = carried + fresh
    candidates, deferred = ordered[: dcfg.max_per_run], ordered[dcfg.max_per_run:]
    if deferred:  # still wanted: picked up by the next runs even when no longer "new"
        store.mark_details_pending(j.job_id for j in deferred)
        notify(f"Углублённый сбор: {len(deferred)} страниц вакансий отложено до следующего запуска "
               f"(лимит collection.details.max_per_run = {dcfg.max_per_run})")
    if not candidates:
        return
    notify(f"Углублённый сбор: страниц вакансий к открытию — {len(candidates)}")
    for i, job in enumerate(candidates):
        result.details_attempted += 1
        try:
            driver.pause_between_pages()
            driver.goto(job.url)
            check_page(driver)
            ready = driver.body_contains_any(DETAIL_MARKERS, cfg.browser.cards_wait_seconds)
            html = driver.html()
            check_page(driver, html)
            details = validated_details(job, driver.current_url(), html, ready, now)
            store.update_job_details(details, run_id, now)
            result.details_fetched += 1
        except (PassAborted, BrowserClosedError):
            raise
        except Exception as exc:  # job data untouched; retried later up to details.max_attempts
            raise_if_blocked(driver)
            store.record_details_failure(job.job_id, short_error(exc))
            result.details_failed += 1
            log.warning("Страница вакансии %s: %s", job.job_id, short_error(exc),
                        exc_info=not isinstance(exc, DetailsRejected))
        if (i + 1) % 10 == 0:
            notify(f"    открыто {i + 1}/{len(candidates)}")
