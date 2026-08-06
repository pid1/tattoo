"""youtube adapter (plan §2).

channel feeds at youtube.com/feeds/videos.xml?channel_id=UC... (last 15
uploads -- sufficient at daily cadence). enrich() batches Data API
videos.list calls (1 quota unit per 50 videos against the free 10k/day
allowance) to get duration and description, dropping shorts and unaired
premieres. content() is the transcript.

transcript fetches originate from the residential ip: serialized, jittered,
and the first blocked-looking failure aborts all remaining transcript
fetches for the run (TransientlyBlocked) -- getting the household ip
throttled affects everything, not just this project. items left without
content are retried on later runs by the acquire stage.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import UTC, datetime

import feedparser

from tattoo.sources import base

VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"

SHORT_MAX_S = 120  # below this it's a Short; drop at enrich (plan §2)
DEFAULT_DURATION_CEILING_S = 5400  # 90min; longer blows context and budget
DEFAULT_TRANSCRIPTS_PER_RUN = 10

# transcript error class names that mean "this video will never have a
# usable transcript" -- degrade immediately rather than retrying forever
_PERMANENT_TRANSCRIPT_ERRORS = {
    "TranscriptsDisabled",
    "NoTranscriptFound",
    "VideoUnavailable",
    "VideoUnplayable",
    "AgeRestricted",
    "InvalidVideoId",
}
# names that mean youtube is pushing back at the network level: stop now
_BLOCKED_TRANSCRIPT_ERRORS = {"IpBlocked", "RequestBlocked", "PoTokenRequired"}

_TRANSCRIPT_POLITENESS_RANGE_S = (2.0, 5.0)


class TransientlyBlocked(RuntimeError):
    """youtube is rate-limiting or blocking the residential ip; the caller
    must stop all further transcript fetches this run."""


def poll(source) -> dict:
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
        raise RuntimeError(f"unparseable feed: {parsed.bozo_exception}")

    entries = []
    for entry in parsed.entries:
        video_id = (entry.get("yt_videoid") or "").strip()
        if not video_id:
            match = re.search(r"yt:video:([\w-]+)", entry.get("id") or "")
            video_id = match.group(1) if match else ""
        if not video_id:
            continue
        entries.append(
            {
                "external_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": (entry.get("title") or "").strip() or "(untitled)",
                "author": (entry.get("author") or "").strip() or None,
                "published_at": _published_iso(entry),
                "video_id": video_id,
            }
        )
    return {
        "entries": entries,
        "etag": result.etag,
        "last_modified": result.last_modified,
        "not_modified": False,
    }


def enrich(entries: list[dict], api_key: str | None = None) -> list[dict]:
    """attach duration/description via Data API; drop shorts and unaired
    premieres. without an api key the entries pass through unfiltered --
    a missing key must not stall the pipeline."""
    if not entries:
        return entries
    if not api_key:
        return entries

    by_id = {e["video_id"]: e for e in entries}
    meta: dict[str, dict] = {}
    ids = list(by_id)
    for start in range(0, len(ids), 50):  # api maximum is 50 ids per call
        batch = ",".join(ids[start : start + 50])
        resp = base.get_json(f"{VIDEOS_API}?part=contentDetails,snippet&id={batch}&key={api_key}")
        for video in resp.get("items", []):
            meta[video["id"]] = video

    kept = []
    for video_id, entry in by_id.items():
        video = meta.get(video_id)
        if video is None:
            continue  # deleted or private between feed and enrich
        duration_s = _iso8601_duration_s(video["contentDetails"].get("duration", ""))
        live_state = video["snippet"].get("liveBroadcastContent", "none")
        if live_state != "none":
            continue  # live or upcoming premiere: no transcript to fetch yet
        if 0 < duration_s < SHORT_MAX_S:
            continue  # shorts are engagement bait by format
        entry["enrich_meta"] = {
            "duration_s": duration_s,
            "description": (video["snippet"].get("description") or "")[:5000],
        }
        kept.append(entry)
    return kept


def content(item, entry: dict | None = None) -> dict:
    """transcript, normalized to plain text with second-offset markers so
    extraction locators can cite into the video. raises TransientlyBlocked
    when youtube pushes back -- the caller stops fetching."""
    video_id = item["external_id"]
    meta = _enrich_meta(item)
    duration_s = meta.get("duration_s") or 0
    description = meta.get("description") or ""

    if duration_s > DEFAULT_DURATION_CEILING_S:
        # over the ceiling: judge the description instead, marked degraded
        return {
            "text": description or item["title"],
            "method": "summary_fallback",
            "degraded": True,
            "final_url": None,
        }

    time.sleep(random.uniform(*_TRANSCRIPT_POLITENESS_RANGE_S))
    try:
        snippets = _fetch_transcript(video_id)
    except Exception as e:
        name = type(e).__name__
        if name in _BLOCKED_TRANSCRIPT_ERRORS or "429" in str(e):
            raise TransientlyBlocked(f"{name}: {e}") from e
        if name in _PERMANENT_TRANSCRIPT_ERRORS:
            return {
                "text": description or item["title"],
                "method": "summary_fallback",
                "degraded": True,
                "final_url": None,
            }
        raise  # unknown failure: leave the item for the retry pass

    return {
        "text": _format_transcript(snippets),
        "method": "transcript",
        "degraded": False,
        "final_url": None,
    }


def _fetch_transcript(video_id: str):
    # lazy import: the library is only needed when a transcript is fetched
    from youtube_transcript_api import YouTubeTranscriptApi

    return YouTubeTranscriptApi().fetch(video_id)


def _format_transcript(snippets) -> str:
    """plain text with [NNNs] markers roughly every 30 seconds -- enough
    granularity for citations without drowning the model in timestamps."""
    parts: list[str] = []
    last_marker = -1000.0
    for snippet in snippets:
        start = getattr(snippet, "start", None)
        text = getattr(snippet, "text", None)
        if start is None or text is None:  # dict-shaped snippets in tests
            start, text = snippet["start"], snippet["text"]
        text = text.strip()
        if not text:
            continue
        if start - last_marker >= 30:
            parts.append(f"[{int(start)}s]")
            last_marker = start
        parts.append(text)
    return " ".join(parts)


def _enrich_meta(item) -> dict:
    try:
        return json.loads(item["enrich_meta"] or "{}")
    except ValueError, KeyError:
        return {}


def _iso8601_duration_s(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _published_iso(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed_time = entry.get(key)
        if parsed_time:
            return datetime(*parsed_time[:6], tzinfo=UTC).isoformat(timespec="seconds")
    return None
