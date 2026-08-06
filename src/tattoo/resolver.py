"""paste-anything source resolution (plan §8): one input field, everything
worked out server-side. accepts whatever the phone's share sheet produces
and returns a confirmation card -- title, detected type, posting frequency,
recent entries, and the content situation -- so the wrong thing never gets
added from an ambiguous handle.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin

import feedparser

from tattoo.sources import base, web

CHANNELS_API = "https://www.googleapis.com/youtube/v3/channels"
VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"

_FEED_MIME_TYPES = {"application/rss+xml", "application/atom+xml", "application/feed+json"}


class ResolveError(Exception):
    """user-facing resolution failure; the message is shown on the card."""


def resolve(text: str, youtube_api_key: str | None) -> dict:
    """returns either {"card": {...}} or {"choices": [{title, url}, ...]}
    when a site advertises several feeds."""
    text = (text or "").strip()
    if not text:
        raise ResolveError("nothing to resolve")

    if _looks_like_youtube(text):
        feed_url = _resolve_youtube(text, youtube_api_key)
        return {"card": _card_from_feed(feed_url, "youtube")}

    url = text if re.match(r"^https?://", text, re.IGNORECASE) else f"https://{text}"
    result = base.fetch(url)
    body = result.body

    parsed = feedparser.parse(body)
    if parsed.entries or (parsed.get("version") and not parsed.bozo):
        return {"card": _card_from_parsed(url, "web", parsed, body)}

    # not a feed: discover <link rel="alternate"> candidates in the page head
    candidates = _discover_feeds(body.decode("utf-8", errors="replace"), result.final_url or url)
    if not candidates:
        raise ResolveError("no feed found at that address")
    if len(candidates) > 1:
        return {"choices": candidates}
    return {"card": _card_from_feed(candidates[0]["url"], "web")}


# -- youtube resolution -------------------------------------------------------


def _looks_like_youtube(text: str) -> bool:
    return "youtube.com" in text or "youtu.be" in text or text.startswith("@")


def _resolve_youtube(text: str, api_key: str | None) -> str:
    channel_id = None

    match = re.search(r"channel_id=(UC[\w-]+)", text) or re.search(r"/channel/(UC[\w-]+)", text)
    if match:
        channel_id = match.group(1)

    if channel_id is None:
        # a watch link usually arrives from the share sheet: resolve the
        # video to its channel (plan §8)
        match = re.search(r"[?&]v=([\w-]{6,})", text) or re.search(r"youtu\.be/([\w-]{6,})", text)
        if match:
            if not api_key:
                raise ResolveError("resolving a video link needs the youtube api key")
            resp = base.get_json(f"{VIDEOS_API}?part=snippet&id={match.group(1)}&key={api_key}")
            items = resp.get("items", [])
            if not items:
                raise ResolveError("video not found")
            channel_id = items[0]["snippet"]["channelId"]

    if channel_id is None:
        match = re.search(r"(?:youtube\.com/)?(@[\w.-]+)", text)
        if match:
            if not api_key:
                raise ResolveError("resolving a handle needs the youtube api key")
            resp = base.get_json(f"{CHANNELS_API}?part=id&forHandle={match.group(1)}&key={api_key}")
            items = resp.get("items", [])
            if not items:
                raise ResolveError(f"no channel found for {match.group(1)}")
            channel_id = items[0]["id"]

    if channel_id is None:
        raise ResolveError("could not work out a channel from that input")
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


# -- feed discovery -----------------------------------------------------------


class _FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._in_head = True  # be lenient: some pages put links after head

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        attr = dict(attrs)
        if (attr.get("rel") or "").lower() != "alternate":
            return
        if (attr.get("type") or "").lower() not in _FEED_MIME_TYPES:
            return
        if attr.get("href"):
            self.links.append({"title": attr.get("title") or attr["href"], "href": attr["href"]})


def _discover_feeds(html: str, base_url: str) -> list[dict]:
    parser = _FeedLinkParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    seen = set()
    candidates = []
    for link in parser.links:
        url = urljoin(base_url, link["href"])
        if url in seen:
            continue
        seen.add(url)
        candidates.append({"title": link["title"], "url": url})
    return candidates


# -- confirmation card --------------------------------------------------------


def _card_from_feed(feed_url: str, source_type: str) -> dict:
    result = base.fetch(feed_url)
    parsed = feedparser.parse(result.body)
    if parsed.bozo and not parsed.entries:
        raise ResolveError(f"feed at {feed_url} did not parse")
    return _card_from_parsed(feed_url, source_type, parsed, result.body)


def _card_from_parsed(feed_url: str, source_type: str, parsed, body: bytes) -> dict:
    entries = parsed.entries or []
    recent = [(e.get("title") or "(untitled)").strip() for e in entries[:5]]

    # posting frequency over the entries the feed exposes -- an immediate
    # sense of whether the volume is manageable (plan §8)
    dates = sorted(
        datetime(*e[key][:6], tzinfo=UTC)
        for e in entries
        for key in ("published_parsed",)
        if e.get(key)
    )
    per_month = None
    if len(dates) >= 2:
        span_days = max((dates[-1] - dates[0]).days, 1)
        per_month = round(len(dates) / span_days * 30, 1)
    elif len(dates) == 1 and dates[0] > datetime.now(UTC) - timedelta(days=30):
        per_month = 1.0

    card = {
        "type": source_type,
        "feed_url": feed_url,
        "title": (parsed.feed.get("title") or feed_url).strip(),
        "site_url": (parsed.feed.get("link") or "").strip() or None,
        "recent": recent,
        "per_month": per_month,
        "suggested_cap": 5 if source_type == "youtube" else 10,
    }
    if source_type == "web":
        card["content_situation"] = _content_situation(entries)
    return card


def _content_situation(entries) -> str:
    """predicts both quality and cost (plan §8)."""
    if not entries:
        return "empty feed"
    first = entries[0]
    body = ""
    for candidate in first.get("content") or []:
        body = candidate.get("value") or ""
        if body:
            break
    if body and len(base.strip_html(body)) >= web.FULL_CONTENT_MIN_CHARS:
        return "full content in feed"
    if first.get("summary"):
        return "summary only — articles will be fetched and extracted"
    return "no body text — articles will be fetched and extracted"
