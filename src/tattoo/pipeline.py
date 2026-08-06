"""pipeline orchestrator (plan §3), reveille build.py's failure contract:
a source failure is data, not an exception; the run fails only on config
or write errors -- and even then it fails *as data* in the runs table, with
a low-priority failure push.

six stages: poll -> diff -> enrich -> acquire -> judge+extract -> render+
notify. stages 1-4 dispatch to the source adapters; the gate and renderer
are source-agnostic. reprocess runs re-judge cached content against the
current prompts without refetching anything (plan §8) -- that is the whole
point of caching content.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tattoo import database, judge, notifier, render, store
from tattoo.log import log
from tattoo.sources import base as source_base
from tattoo.sources import web, youtube
from tattoo.sources.youtube import TransientlyBlocked

ADAPTERS = {"web": web, "youtube": youtube}

# items that failed acquisition are retried on later runs, but only this
# long -- after that the moment has passed and the feed summary suffices
ACQUIRE_RETRY_DAYS = 3


def run(reason: str = "scheduled") -> None:
    conn = database.connect()
    judge.ensure_prompts(conn)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (started_at,))
    run_id = cur.lastrowid
    conn.commit()
    log("pipeline", "run started", run_id=run_id, reason=reason)

    status = "ok"
    counts = {"seen": 0, "judged": 0, "passed": 0}
    now = datetime.now(store.local_tz(conn))
    reprocess = reason == "reprocess"
    try:
        if not reprocess:
            counts["seen"], new_entries = _poll_stage(conn, now)
            _acquire_stage(conn, now, new_entries)
        try:
            counts["judged"], counts["passed"] = _judge_stage(conn, run_id, now, reprocess)
        except judge.BudgetExceeded as e:
            # abort loudly (plan §9), but still render what got through
            status = "aborted_budget"
            log("pipeline", f"aborted: {e}", level="error", run_id=run_id)
            _notify_failure(conn, str(e), now)

        sections = _sections_for_today(conn, now)
        page = render.write_pages(now, {"sections": sections, "shadow_mode": _shadow(conn)})
        log("pipeline", "pages written", run_id=run_id, path=str(page))

        if status == "ok":
            _notify(conn, _digest(conn, sections), now)
        if reason == "scheduled":
            _prune_retention(conn, now)
    except Exception as e:
        status = "failed"
        error = f"{type(e).__name__}: {e}"
        log("pipeline", f"run failed: {error}", level="error", run_id=run_id)
        _notify_failure(conn, error, now)
    finally:
        finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, items_seen = ?, items_judged = ?,"
            " items_passed = ? WHERE id = ?",
            (finished_at, status, counts["seen"], counts["judged"], counts["passed"], run_id),
        )
        conn.commit()
        conn.close()
    log("pipeline", "run finished", run_id=run_id, status=status, **counts)


def reprocess() -> None:
    """re-judge today's cached content against the current prompts, no
    refetching. turns criteria iteration into seconds (plan §8)."""
    run("reprocess")


# -- stage 1+2: poll and diff --------------------------------------------


def _poll_stage(conn, now: datetime) -> tuple[int, list]:
    """poll every enabled source and ingest new items. returns
    (new_count, [(item_id, source_row, entry), ...]) for the acquire stage.
    a single failing source is recorded on its row and skipped, never fatal."""
    total_new = 0
    new_entries: list = []
    sources = conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY id").fetchall()
    for src in sources:
        adapter = ADAPTERS.get(src["type"])
        if adapter is None:
            log("poll", "unknown source type, skipping", level="warn", type=src["type"])
            continue

        envelope = source_base.safe(lambda s=src, a=adapter: a.poll(s))
        polled_at = datetime.now(UTC).isoformat(timespec="seconds")
        if envelope["status"] != "ok":
            conn.execute(
                "UPDATE sources SET last_polled_at = ?, last_poll_status = ? WHERE id = ?",
                (polled_at, envelope["error"], src["id"]),
            )
            conn.commit()
            log(
                "poll",
                "source failed",
                level="warn",
                source=src["display_name"],
                error=envelope["error"],
            )
            continue

        data = envelope["data"]
        entries = _enrich_entries(conn, src, adapter, data["entries"])
        ingested = _ingest(conn, src, entries, now)
        conn.execute(
            "UPDATE sources SET etag = ?, last_modified = ?, last_polled_at = ?,"
            " last_poll_status = 'ok' WHERE id = ?",
            (data["etag"], data["last_modified"], polled_at, src["id"]),
        )
        conn.commit()
        total_new += len(ingested)
        new_entries.extend((item_id, src, entry) for item_id, entry in ingested)
        log(
            "poll",
            "polled",
            source=src["display_name"],
            new_items=len(ingested),
            not_modified=data["not_modified"],
        )
    return total_new, new_entries


def _enrich_entries(conn, src, adapter, entries: list[dict]) -> list[dict]:
    """stage 3, folded into the poll loop: enrich is cheap metadata and its
    failure should degrade (unfiltered entries), not fail the source."""
    if not entries:
        return entries
    try:
        api_key = store.get_secret(conn, "youtube_api_key") if src["type"] == "youtube" else None
        if src["type"] == "youtube" and not api_key:
            log(
                "poll",
                "youtube api key not configured; shorts/premieres not filtered",
                level="warn",
                source=src["display_name"],
            )
        return adapter.enrich(entries, api_key)
    except Exception as e:
        log(
            "poll",
            f"enrich failed, continuing unfiltered: {type(e).__name__}: {e}",
            level="warn",
            source=src["display_name"],
        )
        return entries


def _ingest(conn, src, entries: list[dict], now: datetime) -> list[tuple[int, dict]]:
    existing = {
        r["external_id"]
        for r in conn.execute("SELECT external_id FROM items WHERE source_id = ?", (src["id"],))
    }
    seen_batch: set[str] = set()
    fresh = []
    for entry in entries:
        eid = entry["external_id"]
        if eid in existing or eid in seen_batch:
            continue
        seen_batch.add(eid)
        fresh.append(entry)

    # daily cap counts everything already ingested today, not just this run,
    # so re-runs cannot multiply the budget (plan §3)
    day_start = _local_day_start_utc(now)
    used_today = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id = ? AND first_seen_at >= ?",
        (src["id"], day_start),
    ).fetchone()["n"]
    budget = max(0, src["daily_item_cap"] - used_today)

    # newest first so the cap keeps the most recent items
    fresh.sort(key=lambda e: e["published_at"] or "", reverse=True)
    taken = fresh[:budget]
    if len(taken) < len(fresh):
        # no silent caps: dropped items must be visible in the log
        log(
            "poll",
            "daily cap reached, dropping items",
            level="warn",
            source=src["display_name"],
            dropped=len(fresh) - len(taken),
            cap=src["daily_item_cap"],
        )

    first_seen = datetime.now(UTC).isoformat(timespec="seconds")
    ingested = []
    for entry in taken:
        cur = conn.execute(
            "INSERT INTO items (source_id, external_id, canonical_url, title, normalized_title,"
            " author, published_at, first_seen_at, enrich_meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                src["id"],
                entry["external_id"],
                entry["url"],
                entry["title"],
                _normalize_title(entry["title"]),
                entry["author"],
                entry["published_at"],
                first_seen,
                json.dumps(entry.get("enrich_meta") or {}),
            ),
        )
        ingested.append((cur.lastrowid, entry))
    conn.commit()
    return ingested


# -- stage 4: acquire ------------------------------------------------------


def _acquire_stage(conn, now: datetime, new_entries: list) -> None:
    """persist content for items that lack it. in-run entries carry the
    feed body (not persisted); older content-less items within the retry
    window get the fetch-and-extract path only."""
    entry_by_item = {item_id: entry for item_id, _, entry in new_entries}
    cutoff = (now.astimezone(UTC) - timedelta(days=ACQUIRE_RETRY_DAYS)).isoformat(
        timespec="seconds"
    )
    pending = conn.execute(
        "SELECT i.*, s.type AS source_type, s.display_name AS source_name"
        " FROM items i JOIN sources s ON s.id = i.source_id"
        " LEFT JOIN content c ON c.item_id = i.id"
        " WHERE c.item_id IS NULL AND i.first_seen_at >= ? AND s.enabled = 1"
        " ORDER BY i.id",
        (cutoff,),
    ).fetchall()

    youtube_blocked = False
    for item in pending:
        adapter = ADAPTERS.get(item["source_type"])
        if adapter is None:
            continue
        if item["source_type"] == "youtube" and youtube_blocked:
            continue  # first block aborts all remaining transcript fetches
        try:
            result = adapter.content(item, entry_by_item.get(item["id"]))
        except TransientlyBlocked as e:
            youtube_blocked = True
            log(
                "acquire",
                f"youtube blocking detected, halting transcript fetches: {e}",
                level="warn",
            )
            continue
        except Exception as e:
            # left without a content row: retried next run inside the window
            log(
                "acquire",
                f"failed: {type(e).__name__}: {e}",
                level="warn",
                item_id=item["id"],
                source=item["source_name"],
            )
            continue

        if not (result["text"] or "").strip():
            log("acquire", "no text available", level="warn", item_id=item["id"])
            continue
        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO content (item_id, text, method, fetched_at) VALUES (?, ?, ?, ?)",
            (item["id"], result["text"], result["method"], fetched_at),
        )
        final_url = result.get("final_url")
        conn.execute(
            "UPDATE items SET degraded = ?, canonical_url = COALESCE(?, canonical_url)"
            " WHERE id = ?",
            (int(result["degraded"]), final_url, item["id"]),
        )
        conn.commit()
        # acquisition method logged per item so bad extraction is visible in
        # grafana rather than mysterious (plan §10)
        log(
            "acquire",
            "content stored",
            item_id=item["id"],
            source=item["source_name"],
            method=result["method"],
            chars=len(result["text"]),
        )


# -- stage 5: judge and extract ---------------------------------------------


def _judge_stage(conn, run_id: int, now: datetime, reprocess: bool) -> tuple[int, int]:
    """triage items with content; extract survivors. normal runs judge only
    the not-yet-judged; reprocess re-judges everything from today against
    the current prompts. requires an api key -- without one the stage skips
    entirely (M0-M3 behavior, plan §11)."""
    if not store.get_secret(conn, "anthropic_api_key"):
        log("judge", "anthropic api key not configured; gate skipped")
        return 0, 0

    day_start = _local_day_start_utc(now)
    if reprocess:
        where = "i.first_seen_at >= ?"
    else:
        where = (
            "i.first_seen_at >= ? AND NOT EXISTS (SELECT 1 FROM judgments j WHERE j.item_id = i.id)"
        )
    rows = conn.execute(
        f"SELECT i.id AS item_id FROM items i JOIN content c ON c.item_id = i.id"
        f" JOIN sources s ON s.id = i.source_id WHERE {where} AND s.enabled = 1 ORDER BY i.id",
        (day_start,),
    ).fetchall()

    judged = passed = 0
    for row in rows:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (row["item_id"],)).fetchone()
        content_row = conn.execute(
            "SELECT * FROM content WHERE item_id = ?", (item["id"],)
        ).fetchone()
        source = conn.execute("SELECT * FROM sources WHERE id = ?", (item["source_id"],)).fetchone()
        try:
            verdict = judge.triage_item(conn, run_id, item, content_row, source)
        except judge.BudgetExceeded:
            raise
        except Exception as e:
            # one bad response must not sink the batch
            log(
                "judge",
                f"triage failed: {type(e).__name__}: {e}",
                level="warn",
                item_id=item["id"],
            )
            continue
        judged += 1
        log(
            "judge",
            "triaged",
            item_id=item["id"],
            source=source["display_name"],
            score=verdict["score"],
            passed=verdict["passed"],
        )
        if not verdict["passed"]:
            continue
        passed += 1
        try:
            judge.extract_item(conn, run_id, item, content_row, source)
        except judge.BudgetExceeded:
            raise
        except Exception as e:
            log(
                "judge",
                f"extraction failed: {type(e).__name__}: {e}",
                level="warn",
                item_id=item["id"],
            )
    return judged, passed


# -- stage 6 inputs: sections and digest ----------------------------------


def _shadow(conn) -> bool:
    return store.get_setting(conn, "shadow_mode") == "true"


def _sections_for_today(conn, now: datetime) -> list[dict]:
    """today's items grouped by source with their latest judgment and
    extraction, ordered by score within each group (plan §6). in shadow
    mode rejected items are rendered with score and reason; with the gate
    live they are dropped here."""
    shadow = _shadow(conn)
    day_start = _local_day_start_utc(now)
    items = conn.execute(
        "SELECT i.*, s.display_name AS source_name, s.type AS source_type,"
        " s.threshold AS threshold"
        " FROM items i JOIN sources s ON s.id = i.source_id"
        " WHERE i.first_seen_at >= ?"
        " ORDER BY s.display_name, i.published_at DESC",
        (day_start,),
    ).fetchall()

    grouped: dict[str, list[dict]] = {}
    for item in items:
        judgment = conn.execute(
            "SELECT * FROM judgments WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],)
        ).fetchone()
        passed = bool(judgment["passed"]) if judgment else None
        if judgment and not passed and not shadow:
            continue  # gate live: rejections are invisible by design

        entry = {
            "title": item["title"],
            "url": item["canonical_url"],
            "published_at": item["published_at"],
            "degraded": bool(item["degraded"]),
            "score": judgment["score"] if judgment else None,
            "justification": judgment["justification"] if judgment else None,
            "passed": passed,
            "bluf": None,
            "not_answered": None,
            "specifics": [],
            "findings": [],
        }
        extraction = conn.execute(
            "SELECT * FROM extractions WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],)
        ).fetchone()
        if extraction:
            entry["bluf"] = extraction["bluf"]
            entry["not_answered"] = extraction["not_answered"]
            entry["specifics"] = json.loads(extraction["specifics"] or "[]")
            entry["findings"] = [
                {
                    "text": f["text"],
                    "url": _locator_url(item["source_type"], item["canonical_url"], f["locator"]),
                }
                for f in conn.execute(
                    "SELECT text, locator FROM findings WHERE extraction_id = ? ORDER BY ordinal",
                    (extraction["id"],),
                )
            ]
        grouped.setdefault(item["source_name"], []).append(entry)

    sections = []
    for name, entries in grouped.items():
        entries.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0)))
        sections.append({"source_name": name, "entries": entries})
    return sections


def _locator_url(source_type: str, url: str, locator: str | None) -> str:
    """citations are the payoff (plan §5): the locator becomes a link that
    jumps straight to the substance. the single sanctioned per-type branch
    outside the adapters (plan §2)."""
    if not locator:
        return url
    if source_type == "youtube":
        seconds = locator.rstrip("s")
        if seconds.isdigit():
            separator = "&" if "?" in url else "?"
            return f"{url}{separator}t={seconds}s"
        return url
    if locator.startswith("#"):
        anchor = locator[1:].strip().replace(" ", "-").lower()
        return f"{url}#{anchor}"
    return url


def _digest(conn, sections: list[dict]) -> str:
    """pushover body from the same data as the page (plan §7): bluf and top
    findings with links, so losing the tailnet degrades to 'still got the
    summary'. NSTR on a quiet day, which the notifier skips."""
    gated = [
        (section["source_name"], entry)
        for section in sections
        for entry in section["entries"]
        if entry["passed"]
    ]
    if gated:
        lines = []
        for source_name, entry in gated[:3]:
            lines.append(f"<b>{_esc(source_name)}: {_esc(entry['title'])}</b>")
            if entry["bluf"]:
                lines.append(_esc(entry["bluf"]))
            for finding in entry["findings"][:2]:
                lines.append(f'• {_esc(finding["text"])} <a href="{finding["url"]}">→</a>')
        if len(gated) > 3:
            lines.append(f"(+{len(gated) - 3} more on the dashboard)")
        return "\n".join(lines)

    # gate not yet live (no key / nothing judged): fall back to item counts
    total = sum(len(s["entries"]) for s in sections)
    judged_any = any(e["score"] is not None for s in sections for e in s["entries"])
    if total == 0 or judged_any:
        return "NSTR."  # quiet day, or everything judged and rejected
    lines = [
        f"{total} new item{'s' if total != 1 else ''}"
        f" across {len(sections)} source{'s' if len(sections) != 1 else ''}."
    ]
    for section in sections:
        for entry in section["entries"][:3]:
            lines.append(f"{section['source_name']}: {entry['title']}")
    return "\n".join(lines)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_title(title: str) -> str:
    # stored from day one so a cross-source clustering pass can be added
    # later without a backfill (plan §3)
    return " ".join(title.lower().split())


def _local_day_start_utc(now: datetime) -> str:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start.astimezone(UTC).isoformat(timespec="seconds")


# -- retention ---------------------------------------------------------------


def _prune_retention(conn, now: datetime) -> None:
    """disk-only pruning (plan §4): content text and findings age out;
    items, judgments, and extraction metadata stay queryable forever."""
    raw = store.get_setting(conn, "retention_days", "90")
    try:
        days = int(raw)
    except ValueError:
        days = 90
    if days <= 0:
        return
    cutoff = (now.astimezone(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    pruned_content = conn.execute("DELETE FROM content WHERE fetched_at < ?", (cutoff,)).rowcount
    pruned_findings = conn.execute(
        "DELETE FROM findings WHERE extraction_id IN"
        " (SELECT id FROM extractions WHERE created_at < ?)",
        (cutoff,),
    ).rowcount
    conn.commit()
    if pruned_content or pruned_findings:
        log(
            "pipeline",
            "retention pruned",
            content_rows=pruned_content,
            finding_rows=pruned_findings,
            days=days,
        )


# -- notification ---------------------------------------------------------


def _page_url(conn, now: datetime) -> str | None:
    """dashboard url with the cache-buster (plan §6): mobile safari serves
    stale html on stable urls without it."""
    base = (store.get_setting(conn, "page_url") or "").strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/dashboard/?d={now:%Y-%m-%d}"


def _notify(conn, digest: str, now: datetime) -> None:
    # belt and suspenders: the notifier never raises, and this wrapper makes
    # sure a bug there still can't fail the run after the pages are written
    try:
        notifier.send_pushover(
            digest,
            now,
            token=store.get_secret(conn, "pushover_api_key"),
            user_key=store.get_secret(conn, "pushover_user_key"),
            page_url=_page_url(conn, now),
            html=True,
        )
    except Exception as e:
        log(
            "pipeline",
            f"pushover step crashed unexpectedly: {type(e).__name__}: {e}",
            level="error",
        )


def _notify_failure(conn, error: str, now: datetime) -> None:
    try:
        notifier.send_failure(
            f"run failed: {error}",
            now,
            token=store.get_secret(conn, "pushover_api_key"),
            user_key=store.get_secret(conn, "pushover_user_key"),
        )
    except Exception as e:
        log(
            "pipeline", f"failure push crashed unexpectedly: {type(e).__name__}: {e}", level="error"
        )


if __name__ == "__main__":
    # manual one-shot: PYTHONPATH=src uv run python -m tattoo.pipeline
    database.init_db()
    run("manual")
