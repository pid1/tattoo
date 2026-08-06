"""web adapter contract (plan §2/§3): guid preferred over link for the
dedupe key, canonical links stored, 304 passthrough, dead feeds raise."""

import pytest

from tattoo.sources import base, web


def _rss(items: str) -> bytes:
    return f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>example</title><link>https://example.com</link>
{items}
</channel></rss>""".encode()


def _source(**overrides):
    src = {
        "feed_url": "https://example.com/feed.xml",
        "etag": None,
        "last_modified": None,
    }
    src.update(overrides)
    return src


def _patch_fetch(monkeypatch, result: base.FetchResult):
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return result

    monkeypatch.setattr(web.base, "fetch", fake_fetch)
    return calls


def test_poll_prefers_guid_over_link(monkeypatch):
    body = _rss(
        "<item><guid>tag:example.com,2026:1</guid>"
        "<link>https://example.com/a?utm_source=rss</link>"
        "<title>Post A</title><pubDate>Wed, 05 Aug 2026 12:00:00 GMT</pubDate></item>"
    )
    _patch_fetch(monkeypatch, base.FetchResult(200, body, 'W/"e1"', None))
    result = web.poll(_source())

    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["external_id"] == "tag:example.com,2026:1"
    assert entry["url"] == "https://example.com/a"  # tracking stripped
    assert entry["title"] == "Post A"
    assert entry["published_at"] == "2026-08-05T12:00:00+00:00"
    assert result["etag"] == 'W/"e1"'
    assert result["not_modified"] is False


def test_poll_falls_back_to_canonical_link(monkeypatch):
    body = _rss(
        "<item><link>https://example.com/b?utm_campaign=x</link><title>Post B</title></item>"
    )
    _patch_fetch(monkeypatch, base.FetchResult(200, body, None, None))
    result = web.poll(_source())
    assert result["entries"][0]["external_id"] == "https://example.com/b"


def test_poll_sends_conditional_headers(monkeypatch):
    calls = _patch_fetch(monkeypatch, base.FetchResult(304, b"", 'W/"e1"', "lm-value"))
    result = web.poll(_source(etag='W/"e1"', last_modified="lm-value"))

    assert calls[0][1]["etag"] == 'W/"e1"'
    assert calls[0][1]["last_modified"] == "lm-value"
    # 304: no entries, validators echoed back for re-storing
    assert result["not_modified"] is True
    assert result["entries"] == []
    assert result["etag"] == 'W/"e1"'
    assert result["last_modified"] == "lm-value"


def test_poll_untitled_entry_gets_placeholder(monkeypatch):
    body = _rss("<item><guid>g1</guid><link>https://example.com/c</link></item>")
    _patch_fetch(monkeypatch, base.FetchResult(200, body, None, None))
    assert web.poll(_source())["entries"][0]["title"] == "(untitled)"


def test_poll_garbage_body_raises(monkeypatch):
    _patch_fetch(monkeypatch, base.FetchResult(200, b"not xml at all {", None, None))
    with pytest.raises(RuntimeError, match="unparseable feed"):
        web.poll(_source())
