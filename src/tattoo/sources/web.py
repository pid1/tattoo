"""generic rss/atom adapter (plan §2).

poll() fetches the feed with conditional headers and returns normalized
entries. content() resolves the text to judge in the documented order:
full content already in the feed -> fetch-and-extract (trafilatura) ->
degraded summary fallback.

dedupe key discipline (plan §3): prefer <guid> / atom <id>, which survive
title edits and typo-fix republishes; fall back to the canonical link.
never key on published/updated timestamps.
"""

from __future__ import annotations

import json
import random
import time
from datetime import UTC, datetime

import feedparser

from tattoo.sources import base
from tattoo.urlnorm import canonical_url

# a feed body shorter than this is a teaser, not an article; scoring it as
# full text produces noise in both directions (plan §2)
FULL_CONTENT_MIN_CHARS = 400

# politeness gap between article fetches -- we are a guest on these sites
_POLITENESS_RANGE_S = (0.5, 1.5)


def poll(source) -> dict:
    """source is a sources-table row (or dict). returns
    {entries, etag, last_modified, not_modified}; raises on hard failure
    (the pipeline wraps this in safe())."""
    result = base.fetch(
        source["feed_url"],
        etag=source["etag"],
        last_modified=source["last_modified"],
    )
    if result.status == 304:
        return {
            "entries": [],
            "etag": result.etag,
            "last_modified": result.last_modified,
            "not_modified": True,
        }

    parsed = feedparser.parse(result.body)
    if parsed.bozo and not parsed.entries:
        # bozo with entries is common (minor xml sins); bozo with nothing
        # parseable is a dead feed
        raise RuntimeError(f"unparseable feed: {parsed.bozo_exception}")

    entries = []
    for entry in parsed.entries:
        url = canonical_url(entry.get("link") or "")
        external_id = (entry.get("id") or "").strip() or url
        if not external_id:
            continue  # nothing stable to dedupe on; skip rather than churn
        entries.append(
            {
                "external_id": external_id,
                "url": url,
                "title": (entry.get("title") or "").strip() or "(untitled)",
                "author": (entry.get("author") or "").strip() or None,
                "published_at": _published_iso(entry),
                "feed_body_html": _feed_body(entry),
                "summary_html": entry.get("summary") or "",
            }
        )
    return {
        "entries": entries,
        "etag": result.etag,
        "last_modified": result.last_modified,
        "not_modified": False,
    }


def enrich(entries: list[dict], api_key: str | None = None) -> list[dict]:
    """no-op for web sources."""
    return entries


def content(item, entry: dict | None = None) -> dict:
    """resolve the text to judge (plan §2 order). item is an items-table
    row; entry is the in-run poll entry when available (its feed body is
    not persisted, so retry passes go straight to extraction).

    returns {text, method, degraded, final_url}."""
    # 1. full content already in the feed: best case, zero extra requests
    if entry:
        body_html = entry.get("feed_body_html") or ""
        body_text = base.strip_html(body_html) if body_html else ""
        if len(body_text) >= FULL_CONTENT_MIN_CHARS:
            return {"text": body_text, "method": "feed_body", "degraded": False, "final_url": None}

    # 2. fetch the article and extract readable text
    url = item["canonical_url"]
    extracted = None
    final_url = None
    try:
        time.sleep(random.uniform(*_POLITENESS_RANGE_S))
        result = base.fetch(url)
        final_url = canonical_url(result.final_url) if result.final_url else None
        extracted = _extract_article(result.body)
    except RuntimeError:
        pass  # paywall, bot-wall, dead link: fall through to the summary

    if extracted and len(extracted) >= FULL_CONTENT_MIN_CHARS:
        return {"text": extracted, "method": "extracted", "degraded": False, "final_url": final_url}

    # 3. degraded: judge the feed summary conservatively (plan §2)
    summary_html = (entry or {}).get("summary_html") or ""
    summary_text = base.strip_html(summary_html) if summary_html else ""
    if not summary_text and item["title"]:
        summary_text = item["title"]
    return {
        "text": summary_text,
        "method": "summary_fallback",
        "degraded": True,
        "final_url": final_url,
    }


def _extract_article(body: bytes) -> str | None:
    # trafilatura is the one sanctioned extraction dependency (plan §2);
    # imported lazily because it is heavy and most feeds never need it
    import trafilatura

    html = body.decode("utf-8", errors="replace")
    return trafilatura.extract(html)


def _feed_body(entry) -> str:
    # content:encoded / atom <content> arrive as entry.content
    for candidate in entry.get("content") or []:
        value = candidate.get("value") or ""
        if value:
            return value
    return ""


def _published_iso(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed_time = entry.get(key)
        if parsed_time:
            return datetime(*parsed_time[:6], tzinfo=UTC).isoformat(timespec="seconds")
    return None


def _options(item) -> dict:
    try:
        return json.loads(item["enrich_meta"] or "{}")
    except ValueError, KeyError:
        return {}
