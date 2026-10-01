"""Regression tests for the new-tile (ngm) parsing additions."""

from __future__ import annotations

from upwork_scout.parsing import parse_job_details, parse_search_page

from .conftest import NOW

_BODY = "<h3 data-test='job-title'>{title}</h3><p>Some job description that is long enough to be a description.</p>"


def _tile(extra_attrs: str, inner: str) -> str:
    return f"<div data-test='job-tile' {extra_attrs}>{inner}</div>"


def test_country_tag_wins_over_positional_rule():
    # Old-style tile: country tag present, and the sibling after "Payment verified" is junk.
    html = _tile(
        "data-ev-opening_uid='2105638491020604229'",
        _BODY.format(title="Job A")
        + "<div><span>Payment verified</span></div><div><span>Phone number verified</span></div>"
        + "<div data-test='client-country'><strong>Canada</strong></div>",
    )
    assert parse_search_page(html, NOW)[0].client_country == "Canada"


def test_positional_country_rejects_non_country_text():
    for junk in ("Location United States", "Phone number verified", "No reviews yet"):
        html = _tile(
            "data-ev-opening_uid='2105638491020604229'",
            _BODY.format(title="Job A") + f"<div><span>Payment verified</span></div><div><span>{junk}</span></div>",
        )
        assert parse_search_page(html, NOW)[0].client_country is None, junk


def test_tile_with_job_link_and_opening_uid_is_one_job():
    html = _tile(
        "data-ev-opening_uid='2105638491020604229'",
        "<h3><a href='/jobs/~022105638491020604229'>Linked job</a></h3>"
        "<p>Some job description that is long enough to be a description.</p>",
    )
    jobs = parse_search_page(html, NOW)
    assert [j.job_id for j in jobs] == ["022105638491020604229"]
    assert jobs[0].title == "Linked job"


def test_country_fallback_ignores_excluded_similar_jobs_block():
    html = (
        "<html><body><main><h1>Main job</h1>"
        "<p>Main description that is certainly long enough to count as the job description text here.</p>"
        "<section><a href='/jobs/~021111111111111111111'>Similar job</a>"
        "<div><span>Payment verified</span></div><div><span>Atlantis</span></div></section>"
        "</main></body></html>"
    )
    assert parse_job_details(html, "022222222222222222222", NOW).client_country is None
