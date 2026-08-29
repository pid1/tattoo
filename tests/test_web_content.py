"""web content ladder (plan §2): feed body -> fetch-and-extract -> degraded
summary fallback, in that order, with the degraded flag riding along."""

import pytest

from tattoo.sources import base, web


@pytest.fixture(autouse=True)
def no_politeness_sleep(monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda s: None)


def _item(url="https://example.com/a", title="a post"):
    return {
        "canonical_url": url,
        "title": title,
        "external_id": url,
        "enrich_meta": "{}",
    }


LONG_HTML = "<p>" + ("substantive words here. " * 40) + "</p>"  # ~1000 chars of text


def test_full_content_in_feed_used_directly(monkeypatch):
    def no_fetch(*a, **k):
        raise AssertionError("must not fetch when the feed carries full content")

    monkeypatch.setattr(web.base, "fetch", no_fetch)
    result = web.content(_item(), {"feed_body_html": LONG_HTML, "summary_html": ""})
    assert result["method"] == "feed_body"
    assert result["degraded"] is False
    assert "substantive words" in result["text"]


def test_short_feed_body_falls_through_to_extraction(monkeypatch):
    monkeypatch.setattr(
        web.base,
        "fetch",
        lambda url, **k: base.FetchResult(200, b"<html>page</html>", None, None, url),
    )
    monkeypatch.setattr(
        web, "_extract_article", lambda body: "extracted article text " * 30
    )
    result = web.content(
        _item(), {"feed_body_html": "<p>teaser</p>", "summary_html": ""}
    )
    assert result["method"] == "extracted"
    assert result["degraded"] is False


def test_extraction_failure_degrades_to_summary(monkeypatch):
    def fetch_fails(url, **k):
        raise RuntimeError("HTTP 403 from https://example.com/a: bot-walled")

    monkeypatch.setattr(web.base, "fetch", fetch_fails)
    result = web.content(
        _item(),
        {"feed_body_html": "", "summary_html": "<p>a two sentence summary.</p>"},
    )
    assert result["method"] == "summary_fallback"
    assert result["degraded"] is True
    assert result["text"] == "a two sentence summary."


def test_thin_extraction_degrades(monkeypatch):
    # navigation chrome instead of an article: too short to trust
    monkeypatch.setattr(
        web.base,
        "fetch",
        lambda url, **k: base.FetchResult(200, b"<html>x</html>", None, None, url),
    )
    monkeypatch.setattr(web, "_extract_article", lambda body: "Home | About | Contact")
    result = web.content(
        _item(), {"feed_body_html": "", "summary_html": "<p>summary.</p>"}
    )
    assert result["method"] == "summary_fallback"
    assert result["degraded"] is True


def test_retry_pass_without_entry_uses_extraction(monkeypatch):
    monkeypatch.setattr(
        web.base,
        "fetch",
        lambda url, **k: base.FetchResult(
            200, b"<html>page</html>", None, None, "https://example.com/final"
        ),
    )
    monkeypatch.setattr(web, "_extract_article", lambda body: "long enough text " * 40)
    result = web.content(_item(), None)
    assert result["method"] == "extracted"
    assert result["final_url"] == "https://example.com/final"  # redirect captured


def test_no_entry_no_extraction_falls_back_to_title(monkeypatch):
    def fetch_fails(url, **k):
        raise RuntimeError("network error fetching https://example.com/a: refused")

    monkeypatch.setattr(web.base, "fetch", fetch_fails)
    result = web.content(_item(title="the title"), None)
    assert result["degraded"] is True
    assert result["text"] == "the title"
