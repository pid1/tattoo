"""paste-anything resolution (plan §8): direct feeds, feed discovery from
page heads, youtube handles/channels/watch links, and the confirmation
card fields."""

import pytest

from tattoo import resolver
from tattoo.sources import base

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
 <title>Example Blog</title><link>https://example.com</link>
 <item><guid>g1</guid><title>Post one</title><link>https://example.com/1</link>
   <pubDate>Wed, 05 Aug 2026 12:00:00 GMT</pubDate>
   <description>short</description></item>
 <item><guid>g2</guid><title>Post two</title><link>https://example.com/2</link>
   <pubDate>Wed, 15 Jul 2026 12:00:00 GMT</pubDate>
   <description>short</description></item>
</channel></rss>"""

PAGE_ONE_FEED = b"""<html><head>
<link rel="alternate" type="application/rss+xml" title="Example Feed" href="/feed.xml">
</head><body>hi</body></html>"""

PAGE_TWO_FEEDS = b"""<html><head>
<link rel="alternate" type="application/rss+xml" title="Posts" href="/feed.xml">
<link rel="alternate" type="application/atom+xml" title="Comments" href="/comments.xml">
</head><body>hi</body></html>"""


def _fetches(monkeypatch, responses: dict):
    """map url substring -> body bytes."""

    def fake_fetch(url, **kwargs):
        for fragment, body in responses.items():
            if fragment in url:
                return base.FetchResult(200, body, None, None, url)
        raise RuntimeError(f"HTTP 404 from {url}: ")

    monkeypatch.setattr(resolver.base, "fetch", fake_fetch)


def test_direct_feed_url(monkeypatch):
    _fetches(monkeypatch, {"feed.xml": RSS})
    result = resolver.resolve("https://example.com/feed.xml", None)
    card = result["card"]
    assert card["type"] == "web"
    assert card["title"] == "Example Blog"
    assert card["recent"] == ["Post one", "Post two"]
    assert card["per_month"] is not None
    assert "extract" in card["content_situation"]  # short bodies -> extraction required
    assert card["suggested_cap"] == 10


def test_site_url_discovers_single_feed(monkeypatch):
    _fetches(monkeypatch, {"feed.xml": RSS, "example.com": PAGE_ONE_FEED})
    result = resolver.resolve("example.com", None)  # scheme added server-side
    assert result["card"]["feed_url"].endswith("/feed.xml")


def test_site_with_multiple_feeds_offers_choice(monkeypatch):
    _fetches(monkeypatch, {"example.com": PAGE_TWO_FEEDS})
    result = resolver.resolve("https://example.com", None)
    assert [c["title"] for c in result["choices"]] == ["Posts", "Comments"]
    assert result["choices"][0]["url"] == "https://example.com/feed.xml"


def test_channel_url_resolves_without_api_key(monkeypatch):
    yt_feed = RSS.replace(b"Example Blog", b"Some Channel")
    _fetches(monkeypatch, {"feeds/videos.xml": yt_feed})
    result = resolver.resolve("https://www.youtube.com/channel/UCabc123", None)
    card = result["card"]
    assert card["type"] == "youtube"
    assert card["feed_url"] == "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123"
    assert card["suggested_cap"] == 5


def test_handle_requires_api_key(monkeypatch):
    with pytest.raises(resolver.ResolveError, match="youtube api key"):
        resolver.resolve("@Mike_Tango_Whiskey", None)


def test_handle_resolves_with_api_key(monkeypatch):
    yt_feed = RSS.replace(b"Example Blog", b"MTW")
    _fetches(monkeypatch, {"feeds/videos.xml": yt_feed})
    monkeypatch.setattr(resolver.base, "get_json", lambda url, **k: {"items": [{"id": "UCmtw999"}]})
    result = resolver.resolve("@Mike_Tango_Whiskey", "api-key")
    assert "channel_id=UCmtw999" in result["card"]["feed_url"]


def test_watch_link_resolves_video_to_channel(monkeypatch):
    yt_feed = RSS.replace(b"Example Blog", b"S2")
    _fetches(monkeypatch, {"feeds/videos.xml": yt_feed})
    monkeypatch.setattr(
        resolver.base,
        "get_json",
        lambda url, **k: {"items": [{"snippet": {"channelId": "UCs2s2s2"}}]},
    )
    result = resolver.resolve("https://www.youtube.com/watch?v=dFo9lLCIUM8", "api-key")
    assert "channel_id=UCs2s2s2" in result["card"]["feed_url"]


def test_no_feed_found_raises(monkeypatch):
    _fetches(monkeypatch, {"example.com": b"<html><head></head><body>no feeds</body></html>"})
    with pytest.raises(resolver.ResolveError, match="no feed found"):
        resolver.resolve("https://example.com", None)


def test_resolve_endpoint_maps_errors_to_422(client, monkeypatch):
    monkeypatch.setattr(
        resolver.base,
        "fetch",
        lambda url, **k: (_ for _ in ()).throw(RuntimeError("HTTP 404 from x: ")),
    )
    resp = client.post("/api/sources/resolve", json={"input": "https://nope.example"})
    assert resp.status_code == 422
