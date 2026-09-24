"""Tests for upwork_scout.scraper: the end-to-end read-only pass, driven by FakeDriver.

No test here opens a browser or a socket: FakeDriver serves fixture HTML by URL and the fake
monotonic clock (``fake_clock``) advances by one on every call, so wait loops resolve
immediately instead of sleeping.
"""

from __future__ import annotations

import pytest

from upwork_scout.ids import job_url
from upwork_scout.models import Job, SavedSearch
from upwork_scout.parsing import parse_search_page
from upwork_scout.scraper import (
    DetailsRejected,
    PassAborted,
    PassResult,
    collect_search,
    discover_searches,
    ensure_logged_in,
    fetch_details,
    run_pass,
    validated_details,
)
from upwork_scout.storage import Storage

from .conftest import NOW, FakeDriver, fake_clock, fixture_html

FULL_PAGES = {
    "/nx/find-work": fixture_html("find_work_saved_searches.html"),
    "/nx/find-work/9860550": fixture_html("search_feed_legacy.html"),
    "/nx/find-work/9860538": fixture_html("search_feed_jobtile.html"),
    "/nx/find-work/9860530": fixture_html("search_empty.html"),
    "/jobs/~01c9d8e7f6a5b4c3d2": fixture_html("job_details.html"),
}


class TestFullPass:
    def test_five_jobs_and_ok_search_statuses(self, store: Storage, cfg):
        cfg.collection.load_more_clicks = 0
        cfg.collection.details.mode = "missing"
        run_id = store.start_run(NOW)
        driver = FakeDriver(FULL_PAGES)
        result = PassResult()
        run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())

        assert store.count_jobs() == 5
        statuses = {r.search_id: r.status for r in store.search_runs(run_id)}
        assert all(status == "ok" for status in statuses.values())
        assert len(statuses) == 7

    def test_details_opened_only_for_new_jobs_with_missing_fields(self, store: Storage, cfg):
        cfg.collection.load_more_clicks = 0
        cfg.collection.details.mode = "missing"
        run_id = store.start_run(NOW)
        driver = FakeDriver(FULL_PAGES)
        result = PassResult()
        run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())

        # Only the sparse "Quick question about my website" card is missing key fields and is
        # not hard-excluded; the SEO job is hard-excluded (low budget) and every other card is
        # already complete from its search-page listing.
        visited_job_pages = [v for v in driver.visits if "/jobs/~" in v]
        assert visited_job_pages == ["https://www.upwork.com/jobs/~01c9d8e7f6a5b4c3d2"]
        assert result.details_attempted == 1
        assert result.details_fetched == 1

    def test_second_run_pass_opens_no_detail_pages(self, store: Storage, cfg):
        cfg.collection.load_more_clicks = 0
        cfg.collection.details.mode = "missing"
        run1 = store.start_run(NOW)
        run_pass(FakeDriver(FULL_PAGES), cfg, store, run1, NOW, PassResult(),
                 notify=lambda m: None, clock=fake_clock())

        run2 = store.start_run(NOW)
        driver2 = FakeDriver(FULL_PAGES)
        result2 = PassResult()
        run_pass(driver2, cfg, store, run2, NOW, result2, notify=lambda m: None, clock=fake_clock())

        assert [v for v in driver2.visits if "/jobs/~" in v] == []
        assert result2.details_attempted == 0
        assert store.count_jobs() == 5


class TestSecurityCheckAbortsPass(object):
    PAGES = {
        "/nx/find-work": fixture_html("find_work_saved_searches.html"),
        "/nx/find-work/9860554": fixture_html("search_feed_legacy.html"),
        "/nx/find-work/9860550": fixture_html("cloudflare_challenge.html"),
    }

    def test_aborts_with_security_kind_and_keeps_earlier_data(self, store: Storage, cfg):
        run_id = store.start_run(NOW)
        driver = FakeDriver(self.PAGES)
        result = PassResult()
        try:
            run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "security"

        # 3 jobs came from the first search, already committed before the abort.
        assert store.count_jobs() == 3
        statuses = {r.search_id: r.status for r in store.search_runs(run_id)}
        assert statuses["9860554"] == "ok"
        assert statuses["9860550"] == "aborted"
        assert statuses["9860547"] == "skipped"
        assert statuses["9860530"] == "skipped"


class TestLoginPageAbortsPass:
    PAGES = {
        "/nx/find-work": fixture_html("find_work_saved_searches.html"),
        "/nx/find-work/9860554": fixture_html("search_feed_legacy.html"),
        "/nx/find-work/9860550": fixture_html("login_page.html"),
    }

    def test_aborts_with_auth_kind(self, store: Storage, cfg):
        run_id = store.start_run(NOW)
        driver = FakeDriver(self.PAGES)
        result = PassResult()
        try:
            run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "auth"


class TestOneSearchGotoErrorIsolated:
    PAGES = {
        "/nx/find-work": fixture_html("find_work_saved_searches.html"),
        "/nx/find-work/9860554": fixture_html("search_feed_legacy.html"),
    }

    def test_erroring_search_recorded_others_ok_pass_completes(self, store: Storage, cfg):
        run_id = store.start_run(NOW)
        driver = FakeDriver(self.PAGES, goto_errors={"/nx/find-work/9860550": RuntimeError("boom")})
        result = PassResult()
        # Must not raise: one search's error is isolated.
        run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())

        rows = {r.search_id: r for r in store.search_runs(run_id)}
        assert rows["9860550"].status == "error"
        assert "boom" in rows["9860550"].error
        assert rows["9860554"].status == "ok"
        assert all(r.status == "ok" for sid, r in rows.items() if sid != "9860550")


class TestEnsureLoggedIn:
    def test_login_then_find_work_returns_and_notifies(self, cfg):
        pages = {"/nx/find-work": [fixture_html("login_page.html"), fixture_html("find_work_saved_searches.html")]}
        driver = FakeDriver(pages)
        notes: list[str] = []
        ensure_logged_in(driver, cfg, notify=notes.append, clock=fake_clock())
        assert any("Войдите" in n for n in notes)

    def test_login_timeout_raises_login_timeout(self, cfg):
        cfg.browser.login_timeout_minutes = 0.01
        driver = FakeDriver({"/nx/find-work": fixture_html("login_page.html")})
        try:
            ensure_logged_in(driver, cfg, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "login_timeout"

    def test_non_upwork_url_is_never_interrupted(self, cfg):
        cfg.browser.login_timeout_minutes = 0.2
        driver = FakeDriver({})
        driver.current_url_override = "https://accounts.google.com/o/oauth2/auth"
        try:
            ensure_logged_in(driver, cfg, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "login_timeout"
        # The script must never try to force the user back to Find Work while they are on a
        # legitimate third-party page (e.g. a Google/Apple sign-in flow inside the login wait):
        # only the very first, initial navigation to Find Work should have happened.
        assert driver.visits == ["https://www.upwork.com/nx/find-work/"]


class TestDiscoveryAbort:
    def test_no_saved_searches_aborts_with_discovery_kind(self, store: Storage, cfg):
        run_id = store.start_run(NOW)
        driver = FakeDriver({"/nx/find-work": fixture_html("find_work_no_searches.html")})
        result = PassResult()
        try:
            run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "discovery"


class TestLoadMore:
    def test_click_load_more_stops_at_first_false(self, cfg):
        cfg.collection.load_more_clicks = 5
        search = SavedSearch(search_id="9860550", name="S", url="https://www.upwork.com/nx/find-work/9860550")
        driver = FakeDriver({"9860550": fixture_html("search_feed_legacy.html")},
                            load_more_results=[True, True, False])
        collect_search(driver, cfg, search, NOW, clock=fake_clock())
        assert driver.load_more_calls == 3


class TestGotoErrorEscalatesWhenBlocked:
    """A failed goto for one search must still abort the whole pass if the page left behind in
    the browser turns out to be a challenge/login page (raise_if_blocked), rather than being
    recorded as an ordinary, isolated per-search error."""

    BASE_PAGES = {
        "/nx/find-work": fixture_html("find_work_saved_searches.html"),
        "/nx/find-work/9860554": fixture_html("search_feed_legacy.html"),
    }

    def test_security_page_left_behind_aborts_the_pass(self, store: Storage, cfg):
        pages = {**self.BASE_PAGES, "/nx/find-work/9860550": fixture_html("cloudflare_challenge.html")}
        run_id = store.start_run(NOW)
        driver = FakeDriver(pages, goto_errors={"/nx/find-work/9860550": RuntimeError("net::ERR_ABORTED")})
        result = PassResult()
        try:
            run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "security"

        rows = {r.search_id: r for r in store.search_runs(run_id)}
        assert rows["9860550"].status == "aborted"
        assert rows["9860554"].status == "ok"
        assert store.count_jobs() == 3  # the 1st search's jobs are already stored

    def test_login_page_left_behind_aborts_the_pass(self, store: Storage, cfg):
        pages = {**self.BASE_PAGES, "/nx/find-work/9860550": fixture_html("login_page.html")}
        run_id = store.start_run(NOW)
        driver = FakeDriver(pages, goto_errors={"/nx/find-work/9860550": RuntimeError("net::ERR_ABORTED")})
        result = PassResult()
        try:
            run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "auth"

        rows = {r.search_id: r for r in store.search_runs(run_id)}
        assert rows["9860550"].status == "aborted"
        assert rows["9860554"].status == "ok"

    def test_goto_error_with_a_normal_page_left_behind_is_isolated(self, store: Storage, cfg):
        # Contrast case: the same goto failure, but the page left in the browser is ordinary
        # (no mapping -> the generic OK-ish page) -> recorded as a per-search error, not an abort.
        run_id = store.start_run(NOW)
        driver = FakeDriver(self.BASE_PAGES, goto_errors={"/nx/find-work/9860550": RuntimeError("boom")})
        result = PassResult()
        run_pass(driver, cfg, store, run_id, NOW, result, notify=lambda m: None, clock=fake_clock())

        rows = {r.search_id: r for r in store.search_runs(run_id)}
        assert rows["9860550"].status == "error"
        assert rows["9860554"].status == "ok"


class TestValidatedDetails:
    """validated_details(job, final_url, html, ready, now) — pure function."""

    JOB_ID = "01c9d8e7f6a5b4c3d2"
    TITLE = "Quick question about my website"
    HTML = fixture_html("job_details.html")
    SAME_URL = f"https://www.upwork.com/jobs/~{JOB_ID}"

    def job(self, **kwargs) -> Job:
        kwargs.setdefault("job_id", self.JOB_ID)
        kwargs.setdefault("url", self.SAME_URL)
        kwargs.setdefault("title", self.TITLE)
        return Job(**kwargs)

    def test_not_ready_is_rejected(self):
        with pytest.raises(DetailsRejected):
            validated_details(self.job(), self.SAME_URL, self.HTML, False, NOW)

    def test_final_url_without_a_job_id_is_rejected(self):
        with pytest.raises(DetailsRejected):
            validated_details(self.job(), "https://www.upwork.com/nx/find-work/", self.HTML, True, NOW)

    def test_redirect_to_another_job_with_a_different_title_is_rejected(self):
        other_html = self.HTML.replace(self.TITLE, "Totally different job")
        with pytest.raises(DetailsRejected):
            validated_details(
                self.job(), "https://www.upwork.com/jobs/~01other0000000000", other_html, True, NOW,
            )

    def test_no_longer_available_page_is_rejected(self):
        html = (
            "<html><head><title>This job is no longer available - Upwork</title></head>"
            "<body><main><h1>This job is no longer available</h1></main></body></html>"
        )
        with pytest.raises(DetailsRejected):
            validated_details(self.job(), self.SAME_URL, html, True, NOW)

    def test_page_without_a_title_is_rejected(self):
        html = "<html><body><main><div data-test='Description'>Some text but no h1 at all.</div></main></body></html>"
        with pytest.raises(DetailsRejected):
            validated_details(self.job(), self.SAME_URL, html, True, NOW)

    def test_page_with_title_but_no_description_or_job_type_is_rejected(self):
        html = "<html><body><main><h1>Some Title</h1></main></body></html>"
        with pytest.raises(DetailsRejected):
            validated_details(self.job(), self.SAME_URL, html, True, NOW)

    def test_same_id_is_accepted(self):
        result = validated_details(self.job(), self.SAME_URL, self.HTML, True, NOW)
        assert result.title == self.TITLE

    def test_different_id_is_rejected_even_with_matching_title(self):
        # A matching title is not proof of identity (duplicate job titles are common on
        # Upwork), so any redirect to a different job id is rejected outright — even one with
        # the exact same title as the requested job.
        job = self.job(job_id="01different000000x", title=self.TITLE)
        with pytest.raises(DetailsRejected):
            validated_details(
                job, "https://www.upwork.com/jobs/~02999999999999999999", self.HTML, True, NOW,
            )

    def test_different_id_case_and_space_insensitive_title_is_also_rejected(self):
        job = self.job(job_id="01different000000x", title="quick   QUESTION about MY website")
        with pytest.raises(DetailsRejected):
            validated_details(
                job, "https://www.upwork.com/jobs/~02999999999999999999", self.HTML, True, NOW,
            )


class TestFetchDetailsValidation:
    def _seed_sparse_job(self, store: Storage, cfg) -> tuple[int, Job]:
        run_id = store.start_run(NOW)
        search = SavedSearch(search_id="9860550", name="S", url="https://www.upwork.com/nx/find-work/9860550")
        store.upsert_searches([search], NOW)
        cards = parse_search_page(fixture_html("search_feed_legacy.html"), NOW)
        sparse = [j for j in cards if j.job_id == "01c9d8e7f6a5b4c3d2"]
        store.record_jobs(run_id, search, sparse, NOW)
        return run_id, store.get_job("01c9d8e7f6a5b4c3d2")

    def test_rejected_page_stores_nothing(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        run_id, before = self._seed_sparse_job(store, cfg)
        unavailable_html = (
            "<html><head><title>This job is no longer available - Upwork</title></head>"
            "<body><main><h1>This job is no longer available</h1></main></body></html>"
        )
        driver = FakeDriver({"/jobs/~01c9d8e7f6a5b4c3d2": unavailable_html})
        result = PassResult()
        fetch_details(driver, cfg, store, run_id, NOW, result, notify=lambda m: None)

        after = store.get_record("01c9d8e7f6a5b4c3d2")
        assert after.details_fetched_at is None
        assert after.job.description == before.description  # unchanged ("Need help.")
        assert after.details_attempts == 1
        assert result.details_failed == 1
        assert result.details_fetched == 0

    def test_valid_page_is_stored(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        run_id, _before = self._seed_sparse_job(store, cfg)
        driver = FakeDriver({"/jobs/~01c9d8e7f6a5b4c3d2": fixture_html("job_details.html")})
        result = PassResult()
        fetch_details(driver, cfg, store, run_id, NOW, result, notify=lambda m: None)

        after = store.get_record("01c9d8e7f6a5b4c3d2")
        assert after.details_fetched_at is not None
        assert after.job.job_type == "fixed"
        assert after.job.budget == 250.0
        assert result.details_fetched == 1
        assert result.details_failed == 0


JOB_ID = "01c9d8e7f6a5b4c3d2"
SEARCH = SavedSearch(search_id="9860550", name="S", url="https://www.upwork.com/nx/find-work/9860550")
NOT_READY_HTML = "<html><body><main><p>loading…</p></main></body></html>"  # no DETAIL_MARKERS -> ready=False


def _sparse_job() -> Job:
    cards = parse_search_page(fixture_html("search_feed_legacy.html"), NOW)
    return next(j for j in cards if j.job_id == JOB_ID)


class TestDetailsRetries:
    """collection.details.max_attempts and the retry-until-success behaviour across runs."""

    def test_failed_then_retried_and_succeeds_on_a_later_run(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        cfg.collection.details.only_new = True

        run1 = store.start_run(NOW)
        store.upsert_searches([SEARCH], NOW)
        store.record_jobs(run1, SEARCH, [_sparse_job()], NOW)

        driver1 = FakeDriver({f"/jobs/~{JOB_ID}": NOT_READY_HTML})
        result1 = PassResult()
        fetch_details(driver1, cfg, store, run1, NOW, result1, notify=lambda m: None)

        after_run1 = store.get_record(JOB_ID)
        assert after_run1.details_attempts == 1
        assert after_run1.details_fetched_at is None

        # Run 2: the job is seen again but is not new (only_new=True would normally skip it) —
        # a prior failed attempt must still make it a retry candidate.
        run2 = store.start_run(NOW)
        store.record_jobs(run2, SEARCH, [_sparse_job()], NOW)
        assert store.get_record(JOB_ID).first_seen_run_id == run1  # confirm it is NOT new in run2

        driver2 = FakeDriver({f"/jobs/~{JOB_ID}": fixture_html("job_details.html")})
        result2 = PassResult()
        fetch_details(driver2, cfg, store, run2, NOW, result2, notify=lambda m: None)

        after_run2 = store.get_record(JOB_ID)
        assert after_run2.details_fetched_at is not None
        assert after_run2.details_attempts == 2
        assert result2.details_fetched == 1
        assert [v for v in driver2.visits if "/jobs/~" in v] == [f"https://www.upwork.com/jobs/~{JOB_ID}"]

    def test_page_no_longer_opened_after_max_attempts(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        cfg.collection.details.only_new = True
        cfg.collection.details.max_attempts = 1

        run1 = store.start_run(NOW)
        store.upsert_searches([SEARCH], NOW)
        store.record_jobs(run1, SEARCH, [_sparse_job()], NOW)
        fetch_details(FakeDriver({f"/jobs/~{JOB_ID}": NOT_READY_HTML}), cfg, store, run1, NOW,
                      PassResult(), notify=lambda m: None)
        assert store.get_record(JOB_ID).details_attempts == 1

        # A second run: attempts (1) already meets max_attempts (1) -> never opened again, even
        # though a fresh, perfectly valid page is available this time.
        run2 = store.start_run(NOW)
        store.record_jobs(run2, SEARCH, [_sparse_job()], NOW)
        driver2 = FakeDriver({f"/jobs/~{JOB_ID}": fixture_html("job_details.html")})
        result2 = PassResult()
        fetch_details(driver2, cfg, store, run2, NOW, result2, notify=lambda m: None)

        assert [v for v in driver2.visits if "/jobs/~" in v] == []
        assert result2.details_attempted == 0
        assert store.get_record(JOB_ID).details_fetched_at is None
        assert store.get_record(JOB_ID).details_attempts == 1  # unchanged: never retried

    def test_never_attempted_old_job_is_still_skipped_with_only_new(self, store: Storage, cfg):
        cfg.collection.details.mode = "off"  # run 1: details phase never runs -> attempts stays 0
        cfg.collection.details.only_new = True

        run1 = store.start_run(NOW)
        store.upsert_searches([SEARCH], NOW)
        store.record_jobs(run1, SEARCH, [_sparse_job()], NOW)
        assert store.get_record(JOB_ID).details_attempts == 0

        cfg.collection.details.mode = "missing"
        run2 = store.start_run(NOW)
        store.record_jobs(run2, SEARCH, [_sparse_job()], NOW)
        assert store.get_record(JOB_ID).first_seen_run_id == run1  # not new in run2

        driver2 = FakeDriver({f"/jobs/~{JOB_ID}": fixture_html("job_details.html")})
        result2 = PassResult()
        fetch_details(driver2, cfg, store, run2, NOW, result2, notify=lambda m: None)

        # 0 prior attempts + not new + only_new=True -> not a retry candidate: never opened.
        assert [v for v in driver2.visits if "/jobs/~" in v] == []
        assert result2.details_attempted == 0
        assert store.get_record(JOB_ID).details_fetched_at is None


class _StepClock:
    """A clock driven by an explicit list of values, one per call — deterministic timing for
    discover_searches's stability/quiet-period logic."""

    def __init__(self, values):
        self._values = iter(values)
        self._last = 0.0

    def __call__(self) -> float:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


class _SequenceHtmlDriver:
    """Serves each item of ``pages`` in turn on every call to html() (the last one repeats)."""

    def __init__(self, pages):
        self._pages = list(pages)
        self._i = 0
        self.url = "https://www.upwork.com/nx/find-work/"

    def current_url(self) -> str:
        return self.url

    def html(self) -> str:
        html = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return html

    def wait(self, seconds: float) -> None:
        pass


def _tablist(ids: list[int]) -> str:
    links = "".join(f'<a role="tab" href="/nx/find-work/{i}">s{i}</a>' for i in ids)
    return f"<html><body><main><div role='tablist'>{links}</div></main></body></html>"


class TestDiscoverSearchesStability:
    ALL_IDS = [9860554, 9860550, 9860547, 9860541, 9860538, 9860534, 9860530]

    def test_grows_from_two_to_seven_and_returns_all_seven(self, cfg):
        # First few polls only see 2 links (SPA still rendering); later polls see all 7.
        partial = _tablist(self.ALL_IDS[:2])
        full = fixture_html("find_work_saved_searches.html")
        driver = _SequenceHtmlDriver([partial, partial, partial] + [full] * 20)
        clock = _StepClock(range(0, 60))
        warnings: list[str] = []

        result = discover_searches(driver, cfg, clock=clock, warnings=warnings)

        assert [s.search_id for s in result] == [str(i) for i in self.ALL_IDS]
        assert warnings == []

    def test_never_stabilizes_returns_partial_result_with_warning(self, cfg):
        # The visible set of ids flips between two different sizes on every single poll, so it
        # never stays unchanged for DISCOVERY_QUIET_S: discovery must give up at the deadline.
        class FlappingDriver:
            def __init__(self):
                self.i = 0
                self.url = "https://www.upwork.com/nx/find-work/"

            def current_url(self) -> str:
                return self.url

            def html(self) -> str:
                self.i += 1
                ids = TestDiscoverSearchesStability.ALL_IDS[:2] if self.i % 2 == 0 else \
                    TestDiscoverSearchesStability.ALL_IDS[:3]
                return _tablist(ids)

            def wait(self, seconds: float) -> None:
                pass

        driver = FlappingDriver()
        clock = _StepClock(range(0, 60))
        warnings: list[str] = []

        result = discover_searches(driver, cfg, clock=clock, warnings=warnings)

        assert len(result) in (2, 3)  # whatever the last poll before the deadline happened to show
        assert any("ещё менялся" in w for w in warnings)

    def test_no_links_at_all_raises_discovery_abort(self, cfg):
        class EmptyDriver:
            def current_url(self) -> str:
                return "https://www.upwork.com/nx/find-work/"

            def html(self) -> str:
                return "<html><body><main></main></body></html>"

            def wait(self, seconds: float) -> None:
                pass

        try:
            discover_searches(EmptyDriver(), cfg, clock=_StepClock(range(0, 60)), warnings=[])
            assert False, "expected PassAborted"
        except PassAborted as exc:
            assert exc.kind == "discovery"


def _sparse_new_job(i: int) -> Job:
    """A minimal job missing most key fields, with a stable, sortable canonical id."""
    job_id = f"01{i:018x}"
    return Job(job_id=job_id, url=job_url(job_id), title=f"Sparse job {i}")


class TestDetailsOverflowCarriesAcrossRuns:
    """Jobs deferred by collection.details.max_per_run are remembered (details_pending) and
    retried on later runs, ahead of freshly-new jobs, until every one of them is fetched."""

    def test_overflow_is_fully_drained_over_three_runs(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        cfg.collection.details.only_new = True
        cfg.collection.details.max_per_run = 2

        search = SavedSearch(search_id="9860550", name="S", url="https://www.upwork.com/nx/find-work/9860550")
        store.upsert_searches([search], NOW)
        jobs = [_sparse_new_job(i) for i in range(1, 6)]  # 5 new jobs, all missing key fields
        job_page_html = fixture_html("job_details.html")
        pages = {f"/jobs/~{j.job_id}": job_page_html for j in jobs}

        # --- run 1: only the first 2 (by id order) fit; the other 3 are deferred -------------
        run1 = store.start_run(NOW)
        store.record_jobs(run1, search, jobs, NOW)
        driver1 = FakeDriver(pages)
        result1 = PassResult()
        notes1: list[str] = []
        fetch_details(driver1, cfg, store, run1, NOW, result1, notify=notes1.append)

        visited1 = [v for v in driver1.visits if "/jobs/~" in v]
        assert visited1 == [jobs[0].url, jobs[1].url]
        assert result1.details_fetched == 2
        assert any("3" in n and "отложено" in n for n in notes1)

        recs_after_1 = {j.job_id: store.get_record(j.job_id) for j in jobs}
        assert recs_after_1[jobs[0].job_id].details_fetched_at is not None
        assert recs_after_1[jobs[1].job_id].details_fetched_at is not None
        for j in jobs[2:]:
            assert recs_after_1[j.job_id].details_fetched_at is None
            assert recs_after_1[j.job_id].details_pending is True

        # --- run 2: same jobs, none of them new -> the 3 pending ones are carried, the first 2
        # of them (by id order) are opened -------------------------------------------------
        run2 = store.start_run(NOW)
        store.record_jobs(run2, search, jobs, NOW)
        assert all(store.get_record(j.job_id).first_seen_run_id == run1 for j in jobs)  # none new

        driver2 = FakeDriver(pages)
        result2 = PassResult()
        fetch_details(driver2, cfg, store, run2, NOW, result2, notify=lambda m: None)

        visited2 = [v for v in driver2.visits if "/jobs/~" in v]
        assert visited2 == [jobs[2].url, jobs[3].url]
        assert result2.details_fetched == 2

        rec5_after_2 = store.get_record(jobs[4].job_id)
        assert rec5_after_2.details_fetched_at is None
        assert rec5_after_2.details_pending is True  # still deferred

        # --- run 3: only job 5 is left to carry -> it is finally opened ---------------------
        run3 = store.start_run(NOW)
        store.record_jobs(run3, search, jobs, NOW)
        driver3 = FakeDriver(pages)
        result3 = PassResult()
        fetch_details(driver3, cfg, store, run3, NOW, result3, notify=lambda m: None)

        visited3 = [v for v in driver3.visits if "/jobs/~" in v]
        assert visited3 == [jobs[4].url]
        assert result3.details_fetched == 1

        for j in jobs:
            assert store.get_record(j.job_id).details_fetched_at is not None

        # --- run 4: everything already fetched -> no page is opened again -------------------
        run4 = store.start_run(NOW)
        store.record_jobs(run4, search, jobs, NOW)
        driver4 = FakeDriver(pages)
        result4 = PassResult()
        fetch_details(driver4, cfg, store, run4, NOW, result4, notify=lambda m: None)

        assert [v for v in driver4.visits if "/jobs/~" in v] == []
        assert result4.details_attempted == 0

    def test_carried_jobs_are_visited_before_fresh_new_jobs(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        cfg.collection.details.only_new = True
        cfg.collection.details.max_per_run = 10  # large enough that ordering, not truncation, is on test

        search = SavedSearch(search_id="9860550", name="S", url="https://www.upwork.com/nx/find-work/9860550")
        store.upsert_searches([search], NOW)

        # The fresh job's id sorts alphabetically *before* the carried ones, so seeing it visited
        # last proves the carried-first rule, not an accident of id ordering.
        carried_jobs = [_sparse_new_job(i) for i in (8, 9)]
        fresh_job = _sparse_new_job(0)
        job_page_html = fixture_html("job_details.html")

        run1 = store.start_run(NOW)
        store.record_jobs(run1, search, carried_jobs, NOW)
        store.mark_details_pending(j.job_id for j in carried_jobs)
        store.record_jobs(run1, search, [fresh_job], NOW)  # new in this very run -> "fresh"

        pages = {f"/jobs/~{j.job_id}": job_page_html for j in [*carried_jobs, fresh_job]}
        driver = FakeDriver(pages)
        result = PassResult()
        fetch_details(driver, cfg, store, run1, NOW, result, notify=lambda m: None)

        visited = [v for v in driver.visits if "/jobs/~" in v]
        assert visited == [carried_jobs[0].url, carried_jobs[1].url, fresh_job.url]
        assert result.details_fetched == 3


class TestDeferredJobsOnlyForCurrentFeed:
    """Design decision (4th Codex round, declined on purpose): details are fetched only for jobs
    seen in the current run, because only those are evaluated and shown in its report. A deferred
    job that has left the feed is not opened (no Upwork traffic without effect); it stays pending
    and is opened first if it reappears in a later run."""

    def test_deferred_job_absent_from_feed_is_not_opened_but_stays_pending(self, store: Storage, cfg):
        cfg.collection.details.mode = "missing"
        cfg.collection.details.only_new = True
        cfg.collection.details.max_per_run = 1

        search = SavedSearch(search_id="9860550", name="S", url="https://www.upwork.com/nx/find-work/9860550")
        store.upsert_searches([search], NOW)
        first, deferred = _sparse_new_job(1), _sparse_new_job(2)
        pages = {f"/jobs/~{j.job_id}": fixture_html("job_details.html") for j in (first, deferred)}

        run1 = store.start_run(NOW)
        store.record_jobs(run1, search, [first, deferred], NOW)
        fetch_details(FakeDriver(pages), cfg, store, run1, NOW, PassResult(), notify=lambda m: None)
        assert store.get_record(deferred.job_id).details_pending is True

        # run 2: the deferred job is no longer in the feed -> not opened, still pending
        run2 = store.start_run(NOW)
        store.record_jobs(run2, search, [first], NOW)
        driver2 = FakeDriver(pages)
        fetch_details(driver2, cfg, store, run2, NOW, PassResult(), notify=lambda m: None)
        assert [v for v in driver2.visits if "/jobs/~" in v] == []
        rec = store.get_record(deferred.job_id)
        assert rec.details_pending is True and rec.details_fetched_at is None

        # run 3: it reappears (not new any more) -> carried and opened
        run3 = store.start_run(NOW)
        store.record_jobs(run3, search, [deferred], NOW)
        driver3 = FakeDriver(pages)
        fetch_details(driver3, cfg, store, run3, NOW, PassResult(), notify=lambda m: None)
        assert [v for v in driver3.visits if "/jobs/~" in v] == [deferred.url]
        assert store.get_record(deferred.job_id).details_fetched_at is not None
