"""Tests for upwork_scout.guards: classification of login / security-check / OK pages."""

from __future__ import annotations

from upwork_scout.guards import PageState, detect_page_state

from .conftest import fixture_html


class TestOkPages:
    def test_find_work_with_saved_searches(self):
        html = fixture_html("find_work_saved_searches.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/", html) is PageState.OK

    def test_find_work_no_searches(self):
        html = fixture_html("find_work_no_searches.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/", html) is PageState.OK

    def test_search_feed_legacy(self):
        html = fixture_html("search_feed_legacy.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/9860550", html) is PageState.OK

    def test_search_feed_jobtile(self):
        html = fixture_html("search_feed_jobtile.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/9860538", html) is PageState.OK

    def test_search_empty(self):
        html = fixture_html("search_empty.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/9860530", html) is PageState.OK

    def test_job_details(self):
        html = fixture_html("job_details.html")
        url = "https://www.upwork.com/jobs/~01c9d8e7f6a5b4c3d2"
        assert detect_page_state(url, html) is PageState.OK

    def test_job_about_captchas_is_content_not_a_challenge(self):
        html = fixture_html("job_about_captcha.html")
        url = "https://www.upwork.com/jobs/Captcha-challenge-solver_~01abababababababab"
        assert detect_page_state(url, html) is PageState.OK

    def test_job_slug_mentioning_captcha_with_a_normal_page_is_ok(self):
        html = (
            "<html><body><main><h1>Captcha challenge solver</h1>"
            "<div data-test='Description'>Normal job content.</div></main></body></html>"
        )
        url = "https://www.upwork.com/jobs/Captcha-challenge-solver_~01abababababababab"
        assert detect_page_state(url, html) is PageState.OK


class TestSecurityCheckPages:
    def test_cloudflare_challenge(self):
        html = fixture_html("cloudflare_challenge.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/", html) is PageState.SECURITY_CHECK

    def test_cloudflare_live_ru_localised_interstitial(self):
        html = fixture_html("cloudflare_live_ru.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/", html) is PageState.SECURITY_CHECK

    def test_px_captcha(self):
        html = fixture_html("px_captcha.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/", html) is PageState.SECURITY_CHECK

    def test_text_only_challenge(self):
        html = fixture_html("text_only_challenge.html")
        assert detect_page_state("https://www.upwork.com/nx/find-work/", html) is PageState.SECURITY_CHECK

    def test_device_authorization_path(self):
        state = detect_page_state("https://www.upwork.com/ab/account-security/device-authorization", "<html></html>")
        assert state is PageState.SECURITY_CHECK


class TestLoginPages:
    def test_login_page_fixture(self):
        html = fixture_html("login_page.html")
        url = "https://www.upwork.com/ab/account-security/login"
        assert detect_page_state(url, html) is PageState.LOGIN

    def test_login_path(self):
        state = detect_page_state("https://www.upwork.com/ab/account-security/login", "<html></html>")
        assert state is PageState.LOGIN
