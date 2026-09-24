"""Shared fixtures and helpers for the upwork_scout test suite.

Nothing here makes a network call or launches a browser: HTTP is always mocked with
``httpx.MockTransport`` and the scraper is driven through :class:`FakeDriver`, an in-memory
stand-in for :class:`upwork_scout.scraper.Driver` that serves fixture HTML by URL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from upwork_scout.config import Config, load_config
from upwork_scout.storage import Storage

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Fixed "now" used throughout the suite so relative dates ("2 hours ago", "yesterday", …)
# resolve to deterministic absolute timestamps.
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def fixture_html(name: str) -> str:
    """Read a saved HTML page from tests/fixtures by file name."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """The real config.yaml, with storage/report/logging redirected into tmp_path."""
    config = load_config()
    config.storage.db_path = str(tmp_path / "data" / "upwork.sqlite3")
    config.report.output_dir = str(tmp_path / "outputs")
    config.logging.file = str(tmp_path / "logs" / "upwork-scout.log")
    return config


@pytest.fixture
def store(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "t.sqlite3")
    yield s
    s.close()


class FakeDriver:
    """In-memory stand-in for :class:`upwork_scout.scraper.Driver`.

    ``pages`` maps a URL suffix (matched with ``str.endswith`` after stripping trailing
    slashes, like the reference smoke driver) to either a fixed HTML string or a list of HTML
    strings served in order (the last one repeats) — used to simulate a page changing between
    polls (e.g. a login page that turns into Find Work once the user signs in).

    ``goto_errors`` maps a URL suffix to an exception instance to raise from :meth:`goto`.

    ``current_url_override``, if set, is returned by :meth:`current_url` (and used to look up
    the served page) instead of the URL passed to the last :meth:`goto` — it simulates the
    user's browser being on a page the script never navigated to (e.g. a third-party sign-in
    page during the login wait), without recording a script-initiated visit.
    """

    def __init__(
        self,
        pages: dict[str, str | list[str]],
        *,
        goto_errors: dict[str, BaseException] | None = None,
        load_more_results: list[bool] | None = None,
    ):
        self.pages = dict(pages)
        self.goto_errors = dict(goto_errors or {})
        self.url = "about:blank"
        self.current_url_override: str | None = None
        self.visits: list[str] = []
        self.waits: list[float] = []
        self._html_calls: dict[str, int] = {}
        self.load_more_results = list(load_more_results) if load_more_results is not None else None
        self.load_more_calls = 0

    # --- internals -------------------------------------------------------------------------
    @staticmethod
    def _match(url: str, keys) -> str | None:
        for key in keys:
            if url.rstrip("/").endswith(key.rstrip("/")):
                return key
        return None

    def _match_key(self, url: str) -> str | None:
        return self._match(url, self.pages)

    # --- Driver protocol ---------------------------------------------------------------------
    def goto(self, url: str) -> None:
        self.visits.append(url)
        self.url = url
        error_key = self._match(url, self.goto_errors)
        if error_key is not None:
            raise self.goto_errors[error_key]

    def current_url(self) -> str:
        return self.current_url_override or self.url

    def html(self) -> str:
        key = self._match_key(self.current_url())
        if key is None:
            return "<html><body><main><p>empty</p></main></body></html>"
        value = self.pages[key]
        if isinstance(value, list):
            idx = self._html_calls.get(key, 0)
            self._html_calls[key] = idx + 1
            return value[min(idx, len(value) - 1)]
        return value

    def count_job_links(self) -> int:
        from upwork_scout.parsing import parse_search_page

        return len(parse_search_page(self.html()))

    def count_search_links(self) -> int:
        from upwork_scout.discovery import parse_saved_searches

        return len(parse_saved_searches(self.html()))

    def body_contains_any(self, needles, timeout_s: float) -> bool:
        text = self.html()
        return any(n in text for n in needles)

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def pause_between_pages(self) -> None:
        pass

    def scroll_through(self, max_steps: int = 8) -> None:
        pass

    def click_load_more(self, wait_s: float) -> bool:
        self.load_more_calls += 1
        if self.load_more_results is None:
            return False
        idx = self.load_more_calls - 1
        return self.load_more_results[idx] if idx < len(self.load_more_results) else self.load_more_results[-1]


def fake_clock():
    """A monotonic clock that advances by 1 on every call and never blocks a wait loop."""
    return iter(range(0, 10**6)).__next__
