"""youtube adapter contract (plan §2): video ids as dedupe keys, enrich
drops shorts and premieres, transcripts carry second markers, and the
failure taxonomy maps to degraded / retry / abort correctly."""

import json

import pytest

from tattoo.sources import base, youtube

FEED = """<?xml version="1.0"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
 <title>chan</title>
 <entry>
  <id>yt:video:VID00000001</id><yt:videoId>VID00000001</yt:videoId>
  <title>a video</title>
  <published>2026-08-05T12:00:00+00:00</published>
  <author><name>chan</name></author>
  <link rel="alternate" href="https://www.youtube.com/watch?v=VID00000001"/>
 </entry>
</feed>"""


def _source():
    return {
        "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCx",
        "etag": None,
        "last_modified": None,
    }


def _item(video_id="VID00000001", duration_s=600, description="desc"):
    return {
        "external_id": video_id,
        "canonical_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "a video",
        "enrich_meta": json.dumps(
            {"duration_s": duration_s, "description": description}
        ),
    }


@pytest.fixture(autouse=True)
def no_politeness_sleep(monkeypatch):
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)


def test_poll_uses_video_id_as_external_id(monkeypatch):
    monkeypatch.setattr(
        youtube.base,
        "fetch",
        lambda url, **k: base.FetchResult(200, FEED.encode(), None, None),
    )
    result = youtube.poll(_source())
    entry = result["entries"][0]
    assert entry["external_id"] == "VID00000001"
    assert entry["url"] == "https://www.youtube.com/watch?v=VID00000001"
    assert entry["author"] == "chan"


def _videos_response(items):
    return {"items": items}


def test_enrich_drops_shorts_and_premieres(monkeypatch):
    entries = [
        {"video_id": "LONG", "title": "long"},
        {"video_id": "SHORT", "title": "short"},
        {"video_id": "LIVE", "title": "premiere"},
    ]
    api_items = [
        {
            "id": "LONG",
            "contentDetails": {"duration": "PT28M30S"},
            "snippet": {"liveBroadcastContent": "none", "description": "d"},
        },
        {
            "id": "SHORT",
            "contentDetails": {"duration": "PT45S"},
            "snippet": {"liveBroadcastContent": "none", "description": "d"},
        },
        {
            "id": "LIVE",
            "contentDetails": {"duration": "PT0S"},
            "snippet": {"liveBroadcastContent": "upcoming", "description": "d"},
        },
    ]
    monkeypatch.setattr(
        youtube.base, "get_json", lambda url, **k: _videos_response(api_items)
    )
    kept = youtube.enrich(entries, api_key="k")
    assert [e["video_id"] for e in kept] == ["LONG"]
    assert kept[0]["enrich_meta"]["duration_s"] == 28 * 60 + 30


def test_enrich_without_key_passes_through():
    entries = [{"video_id": "X", "title": "t"}]
    assert youtube.enrich(entries, api_key=None) == entries


def test_iso8601_duration_parse():
    assert youtube._iso8601_duration_s("PT1H2M3S") == 3723
    assert youtube._iso8601_duration_s("PT45S") == 45
    assert youtube._iso8601_duration_s("P1DT1S") == 86401
    assert youtube._iso8601_duration_s("garbage") == 0


def test_content_transcript_with_markers(monkeypatch):
    snippets = [
        {"start": 0.0, "text": "intro words"},
        {"start": 12.0, "text": "more intro"},
        {"start": 45.0, "text": "the first spec is 42mm"},
    ]
    monkeypatch.setattr(youtube, "_fetch_transcript", lambda vid: snippets)
    result = youtube.content(_item())
    assert result["method"] == "transcript"
    assert result["degraded"] is False
    assert "[0s] intro words more intro [45s] the first spec is 42mm" == result["text"]


def test_content_over_duration_ceiling_degrades(monkeypatch):
    def no_fetch(vid):
        raise AssertionError("must not fetch a transcript over the ceiling")

    monkeypatch.setattr(youtube, "_fetch_transcript", no_fetch)
    result = youtube.content(
        _item(duration_s=3 * 3600, description="a three hour podcast")
    )
    assert result["method"] == "summary_fallback"
    assert result["degraded"] is True
    assert result["text"] == "a three hour podcast"


def test_content_permanent_error_degrades(monkeypatch):
    class TranscriptsDisabled(Exception):
        pass

    def raise_disabled(vid):
        raise TranscriptsDisabled("disabled")

    monkeypatch.setattr(youtube, "_fetch_transcript", raise_disabled)
    result = youtube.content(_item(description="the description"))
    assert result["method"] == "summary_fallback"
    assert result["degraded"] is True
    assert result["text"] == "the description"


def test_content_blocked_raises_transiently_blocked(monkeypatch):
    class IpBlocked(Exception):
        pass

    def raise_blocked(vid):
        raise IpBlocked("youtube says no")

    monkeypatch.setattr(youtube, "_fetch_transcript", raise_blocked)
    with pytest.raises(youtube.TransientlyBlocked):
        youtube.content(_item())


def test_content_unknown_error_propagates_for_retry(monkeypatch):
    def raise_unknown(vid):
        raise ConnectionError("flaky network")

    monkeypatch.setattr(youtube, "_fetch_transcript", raise_unknown)
    with pytest.raises(ConnectionError):
        youtube.content(_item())
