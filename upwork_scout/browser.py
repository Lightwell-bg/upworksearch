"""Playwright driver: installed Google Chrome, a separate persistent profile, one tab.

Read-only by construction: the driver can only navigate to https://www.upwork.com URLs,
read the page HTML, scroll, and click a button whose accessible name is exactly
"Load More Jobs". It never fills forms, submits anything, or reads cookies/storage.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .config import Config, validate_profile_dir
from .ids import is_upwork_url

log = logging.getLogger(__name__)

LOAD_MORE_NAME = re.compile(r"^\s*load\s+more\s+jobs?\s*$", re.IGNORECASE)

_JOB_COUNT_JS = r"""() => {
  const re = /\/(?:jobs|job|details|apply)\/(?:[^\/?#\s]*_)?~(0\d[0-9a-z]{14,30})(?![0-9a-z])/i;
  const ids = new Set();
  for (const a of document.querySelectorAll('a[href*="~0"]')) {
    const m = (a.getAttribute('href') || '').match(re);
    if (m) ids.add(m[1].toLowerCase());
  }
  return ids.size;
}"""

_SEARCH_COUNT_JS = r"""() => {
  const ids = new Set();
  for (const a of document.querySelectorAll('a[href*="/nx/find-work/"]')) {
    try {
      const u = new URL(a.getAttribute('href'), location.origin);
      const m = u.pathname.match(/^\/nx\/find-work\/(\d{1,20})\/?$/);
      if (m && /(^|\.)upwork\.com$/.test(u.hostname)) ids.add(m[1]);
    } catch (e) {}
  }
  return ids.size;
}"""

_BODY_HAS_JS = r"""(needles) => {
  const text = (document.body && document.body.innerText) || '';
  return needles.some(n => text.includes(n));
}"""

_SCROLL_JS = r"""() => {
  window.scrollBy(0, Math.round(window.innerHeight * 0.9));
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 4;
}"""


class BrowserClosedError(Exception):
    """The Chrome window/tab was closed while the pass was running."""


class BrowserLaunchError(Exception):
    """Google Chrome could not be started with the scraper profile."""


def _is_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(s in text for s in ("has been closed", "target closed", "browser has disconnected", "target page, context"))


class PlaywrightDriver:
    def __init__(self, page: Page, nav_timeout_s: float, delay_range: tuple[float, float]):
        self.page = page
        self.nav_timeout_ms = int(nav_timeout_s * 1000)
        self.delay_range = delay_range

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PlaywrightError as exc:
            if self.page.is_closed() or _is_closed_error(exc):
                raise BrowserClosedError("окно Chrome закрыто") from exc
            raise

    # --- navigation & reading -------------------------------------------------------------
    def goto(self, url: str) -> None:
        if not is_upwork_url(url):
            raise ValueError(f"навигация разрешена только на https://www.upwork.com: {url}")
        self._call(self.page.goto, url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)

    def current_url(self) -> str:
        if self.page.is_closed():
            raise BrowserClosedError("окно Chrome закрыто")
        return self.page.url

    def _settled(self, fn, *args, budget_s: float = 15.0):
        """Run a page read, riding out navigations ("page is navigating", "execution
        context was destroyed") — e.g. while the user submits the login form."""
        deadline = time.monotonic() + budget_s
        while True:
            try:
                return self._call(fn, *args)
            except BrowserClosedError:
                raise
            except PlaywrightError:
                if time.monotonic() >= deadline:
                    raise
                try:
                    self._call(self.page.wait_for_load_state, "domcontentloaded", timeout=5000)
                except BrowserClosedError:
                    raise
                except PlaywrightError:
                    pass
                self.wait(0.3)

    def html(self) -> str:
        return self._settled(self.page.content)

    def _evaluate(self, script: str, arg=None):
        return self._settled(self.page.evaluate, script, arg)

    def count_job_links(self) -> int:
        return int(self._evaluate(_JOB_COUNT_JS) or 0)

    def count_search_links(self) -> int:
        return int(self._evaluate(_SEARCH_COUNT_JS) or 0)

    def body_contains_any(self, needles: Sequence[str], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self._evaluate(_BODY_HAS_JS, list(needles)):
                return True
            if time.monotonic() >= deadline:
                return False
            self.wait(0.5)

    # --- pacing & interaction -------------------------------------------------------------
    def wait(self, seconds: float) -> None:
        self._call(self.page.wait_for_timeout, max(0, int(seconds * 1000)))

    def pause_between_pages(self) -> None:
        lo, hi = self.delay_range
        self.wait(random.uniform(lo, hi))

    def scroll_through(self, max_steps: int = 8) -> None:
        """Scroll down gradually so lazily rendered cards appear."""
        for _ in range(max_steps):
            if self._evaluate(_SCROLL_JS):
                break
            self.wait(0.35)

    def click_load_more(self, wait_s: float) -> bool:
        """Click "Load More Jobs" once; True if more job cards appeared."""
        button = self.page.get_by_role("button", name=LOAD_MORE_NAME)
        if self._call(button.count) == 0:
            return False
        before = self.count_job_links()
        target = button.first
        self._call(target.scroll_into_view_if_needed, timeout=5000)
        self._call(target.click, timeout=10000)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            self.wait(0.5)
            if self.count_job_links() > before:
                self.wait(0.8)  # let the rest of the batch render
                return True
        return False


@contextmanager
def open_browser(cfg: Config) -> Iterator[PlaywrightDriver]:
    """Start Chrome with the dedicated profile and yield a driver for its single tab."""
    profile = cfg.path(cfg.browser.profile_dir)
    validate_profile_dir(profile)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=cfg.browser.channel,
                headless=False,
                no_viewport=True,
                args=["--start-maximized"],
            )
        except PlaywrightError as exc:
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise BrowserLaunchError(
                f"Не удалось запустить Google Chrome ({first_line}). Проверьте, что Chrome установлен "
                f"и что окно с профилем «{profile}» не осталось открытым от предыдущего запуска."
            ) from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for extra in context.pages[1:]:  # restored tabs: keep exactly one
                extra.close()
            page.set_default_timeout(cfg.browser.navigation_timeout_seconds * 1000)
            yield PlaywrightDriver(page, cfg.browser.navigation_timeout_seconds, cfg.browser.delay_between_pages_seconds)
        finally:
            try:
                context.close()
            except PlaywrightError:
                pass
