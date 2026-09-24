"""Tests for upwork_scout.htmltext.sanitize_for_debug: markup diagnostics without secrets."""

from __future__ import annotations

from upwork_scout.htmltext import sanitize_for_debug

RAW_HTML = """<!DOCTYPE html><html><head><title>t</title>
<script>window.__data = {token:"secret"};</script>
</head><body>
<header><nav><a href="/freelancers/~01aaaabbbbccccdddd">Profile</a></nav></header>
<main>
<script type="application/json">{"csrf":"abc"}</script>
<form id="login" action="/x"><input type="hidden" name="csrf_token" value="SECRETVALUE"></form>
<article data-test="JobTile" onclick="doThing()" style="color:red" data-csrf="zz">
  <h2><a href="/jobs/Title_~01f4a3b2c1d0e9f8a7/" onmouseover="x()">Some job title</a></h2>
  <span data-test="job-description-text">Job description text here.</span>
</article>
<a href="/nx/find-work/9860554">saved search</a>
<a href="/freelancers/~01aaaabbbbccccdddd">other freelancer profile link</a>
<iframe src="https://evil.example.com"></iframe>
</main>
<aside>sidebar</aside>
<footer>footer text</footer>
</body></html>"""


class TestSanitizeForDebug:
    def test_scripts_and_inline_bootstrap_json_removed(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "<script" not in out
        assert "secret" not in out
        assert "csrf" not in out.lower()

    def test_forms_and_hidden_inputs_removed(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "<form" not in out
        assert "<input" not in out
        assert "SECRETVALUE" not in out

    def test_header_nav_aside_footer_removed(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "<header" not in out
        assert "<nav" not in out
        assert "<aside" not in out
        assert "<footer" not in out
        assert "sidebar" not in out
        assert "footer text" not in out

    def test_iframes_removed(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "<iframe" not in out
        assert "evil.example.com" not in out

    def test_event_handlers_and_style_and_data_csrf_attrs_stripped(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "onclick" not in out
        assert "onmouseover" not in out
        assert "style=" not in out
        assert "data-csrf" not in out

    def test_job_card_text_and_data_test_kept(self):
        out = sanitize_for_debug(RAW_HTML)
        assert 'data-test="JobTile"' in out
        assert "Some job title" in out
        assert "Job description text here." in out

    def test_job_link_href_kept(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "~01f4a3b2c1d0e9f8a7" in out

    def test_saved_search_link_href_kept(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "/nx/find-work/9860554" in out

    def test_other_links_lose_their_href_but_keep_text(self):
        out = sanitize_for_debug(RAW_HTML)
        assert "/freelancers/~01aaaabbbbccccdddd" not in out
        assert "other freelancer profile link" in out

    def test_output_is_a_full_html_document(self):
        out = sanitize_for_debug(RAW_HTML)
        assert out.strip().startswith("<!DOCTYPE html>")
        assert "<html>" in out
        assert "<body>" in out
        assert "</html>" in out
