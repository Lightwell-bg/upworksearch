"""Classification of the current page: normal, login (session lost) or security check.

The scraper never tries to solve or bypass a challenge: it only recognises it so the pass
can stop with a clear message. Signals are deliberately specific — a false "security check"
would abort the whole pass, so free-text phrases (which also appear in job descriptions
about CAPTCHAs) count only on small pages that contain no job or saved-search links.
"""

from __future__ import annotations

from enum import Enum
from urllib.parse import urlparse

from .htmltext import block_text, soup_of
from .ids import normalize_job_id, parse_search_id


class PageState(str, Enum):
    OK = "ok"
    LOGIN = "login"
    SECURITY_CHECK = "security_check"


# Only checked inside Upwork's account-security area: a job slug such as
# /jobs/Captcha-challenge-solver_~01… must not look like a challenge.
_ACCOUNT_SECURITY_PREFIX = "/ab/account-security/"
_SECURITY_PATH_MARKERS = ("device-authorization", "secured-action", "captcha", "challenge", "verify")
_LOGIN_PATH_PREFIXES = ("/ab/login", "/login", "/nx/signup", "/signup")

_SECURITY_TITLES = frozenset(
    {
        "just a moment...",
        "just a moment…",
        "just a moment",
        "attention required! | cloudflare",
        "access denied",
        "access to this page has been denied",
        "access to this page has been denied.",
        "one more step",
        "please wait...",
        "security check",
        "human verification",
        # Cloudflare localises the interstitial title to the browser language.
        "один момент…",
        "один момент...",
        "одну минуту…",
        "проверка безопасности",
    }
)
_SECURITY_ELEMENT_IDS = (
    "challenge-form",
    "challenge-stage",
    "challenge-running",
    "cf-challenge-running",
    "cf-wrapper",
    "turnstile-wrapper",
    "px-captcha",
    "px-captcha-wrapper",
    "challenge-error-text",
)
# Seen on the live Upwork interstitial (2026-09): random element ids, a "cf-chl-widget-…"
# input, the "…/orchestrate/chl_page/…" script and Upwork's "up-challenge-container".
# Normal pages also load /cdn-cgi/challenge-platform/scripts/jsd/…, which is NOT a signal.
_SECURITY_SRC_MARKERS = (
    "challenges.cloudflare.com",
    "/orchestrate/chl_page",
    "/orchestrate/captcha",
    "hcaptcha.com",
    "captcha-delivery.com",
    "/recaptcha/api2/bframe",
)
_SECURITY_ID_PREFIXES = ("cf-chl-widget",)
_SECURITY_CLASSES = ("up-challenge-container", "cf-turnstile", "h-captcha")
_SECURITY_TEXT = (
    "verify you are human",
    "verifying you are human",
    "i'm not a robot",
    "i am not a robot",
    "press & hold",
    "press and hold",
    "checking your browser",
    "checking if the site connection is secure",
    "please complete the security check",
    "access to this page has been denied",
    "unusual traffic",
    "are you a robot",
)
_SMALL_PAGE_CHARS = 6000
# Upwork content markers: a job page about CAPTCHAs is content, not a challenge.
_CONTENT_SELECTOR = (
    "[data-test~='Description'], [data-test~='JobTile'], [data-test~='job-description-text'], "
    "[data-ev-sublocation='job_feed_tile']"
)
_LOGIN_FORM_SELECTOR ="input#login_username, input[name='login[username]'], input#login_password, form#login"


def detect_page_state(url: str, html: str) -> PageState:
    path = (urlparse(url or "").path or "").lower()
    if path.startswith("/cdn-cgi/"):
        return PageState.SECURITY_CHECK
    if path.startswith(_ACCOUNT_SECURITY_PREFIX):
        if any(marker in path for marker in _SECURITY_PATH_MARKERS):
            return PageState.SECURITY_CHECK
        return PageState.LOGIN
    if path == "/login" or path.startswith(_LOGIN_PATH_PREFIXES):
        return PageState.LOGIN

    soup = soup_of(html)
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split()).lower()
    if title in _SECURITY_TITLES or title.endswith("| cloudflare"):
        return PageState.SECURITY_CHECK
    for element_id in _SECURITY_ELEMENT_IDS:
        if soup.find(id=element_id) is not None:
            return PageState.SECURITY_CHECK
    if soup.find(class_=lambda c: c in _SECURITY_CLASSES) is not None:
        return PageState.SECURITY_CHECK
    if soup.find(id=lambda i: bool(i) and i.startswith(_SECURITY_ID_PREFIXES)) is not None:
        return PageState.SECURITY_CHECK
    for tag in soup.find_all(["iframe", "script"], src=True):
        src = str(tag.get("src", "")).lower()
        if any(marker in src for marker in _SECURITY_SRC_MARKERS):
            return PageState.SECURITY_CHECK

    if soup.select_one(_LOGIN_FORM_SELECTOR):
        return PageState.LOGIN

    has_content = soup.select_one(_CONTENT_SELECTOR) is not None or any(
        normalize_job_id(a["href"]) or parse_search_id(a["href"]) for a in soup.find_all("a", href=True)
    )
    if not has_content:
        body = soup.body or soup
        text = block_text(body).lower()
        if len(text) <= _SMALL_PAGE_CHARS and any(phrase in text for phrase in _SECURITY_TEXT):
            return PageState.SECURITY_CHECK
    return PageState.OK
