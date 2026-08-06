"""run-now, reprocess, and config export/import (plan §8). 'run now' is the
highest-value control on the page; reprocess turns criteria iteration from
24 hours into seconds by re-judging cached content.
"""

from __future__ import annotations

import sqlite3
import threading
import tomllib
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from tattoo import judge, store
from tattoo.database import get_db
from tattoo.log import log
from tattoo.scheduler import default_scheduler

router = APIRouter(tags=["actions"])


@router.post("/api/run", status_code=202)
def run_now() -> dict:
    default_scheduler.trigger()
    return {"status": "triggered"}


@router.post("/api/reprocess", status_code=202)
def reprocess() -> dict:
    """re-judge cached content against current prompts, in the background --
    a reprocess over a day of items can take minutes of llm calls."""

    def _worker():
        from tattoo import pipeline

        try:
            pipeline.reprocess()
        except Exception as e:
            log("actions", f"reprocess crashed: {type(e).__name__}: {e}", level="error")

    threading.Thread(target=_worker, name="tattoo-reprocess", daemon=True).start()
    return {"status": "reprocessing"}


# -- config export / import ----------------------------------------------------


def _toml_str(value) -> str:
    import json

    return json.dumps(str(value))  # json string escaping is valid toml


@router.get("/api/export/config", response_class=PlainTextResponse)
def export_config(db: sqlite3.Connection = Depends(get_db)) -> str:
    """toml snapshot for backup / version control. secrets are excluded by
    construction (plan §0.4): only PUBLIC_SETTINGS keys are read."""
    judge.ensure_prompts(db)
    lines = [f"# tattoo config export {datetime.now(UTC).isoformat(timespec='seconds')}", ""]
    lines.append("[settings]")
    for key in store.PUBLIC_SETTINGS:
        lines.append(f"{key} = {_toml_str(store.get_setting(db, key, '') or '')}")
    lines.append("")
    lines.append("[prompts]")
    for name in judge.PROMPT_NAMES:
        _, value = judge.current_prompt(db, name)
        lines.append(f"{name} = {_toml_str(value)}")
    for row in db.execute("SELECT * FROM sources ORDER BY id"):
        lines.append("")
        lines.append("[[sources]]")
        lines.append(f"type = {_toml_str(row['type'])}")
        lines.append(f"feed_url = {_toml_str(row['feed_url'])}")
        lines.append(f"display_name = {_toml_str(row['display_name'])}")
        if row["site_url"]:
            lines.append(f"site_url = {_toml_str(row['site_url'])}")
        lines.append(f"criteria = {_toml_str(row['criteria'])}")
        lines.append(f"threshold = {row['threshold']}")
        lines.append(f"daily_item_cap = {row['daily_item_cap']}")
        lines.append(f"enabled = {'true' if row['enabled'] else 'false'}")
    return "\n".join(lines) + "\n"


@router.post("/api/import/config")
def import_config(body: dict, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """idempotent upsert of an exported snapshot: settings by key, sources
    by feed_url, prompts as new versions (history preserved)."""
    raw = body.get("toml")
    if not raw:
        raise HTTPException(status_code=422, detail="body must be {'toml': '<export text>'}")
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise HTTPException(status_code=422, detail=f"toml parse error: {e}") from e

    counts = {"settings": 0, "sources": 0, "prompts": 0}
    for key, value in (parsed.get("settings") or {}).items():
        if key in store.PUBLIC_SETTINGS:
            store.set_setting(db, key, str(value))
            counts["settings"] += 1

    judge.ensure_prompts(db)
    for name, value in (parsed.get("prompts") or {}).items():
        if name in judge.PROMPT_NAMES and str(value).strip():
            _, current = judge.current_prompt(db, name)
            if current != str(value).strip():
                judge.save_prompt(db, name, str(value).strip())
                counts["prompts"] += 1

    now = datetime.now(UTC).isoformat(timespec="seconds")
    for src in parsed.get("sources") or []:
        feed_url = (src.get("feed_url") or "").strip()
        source_type = src.get("type")
        if not feed_url or source_type not in ("web", "youtube"):
            continue
        db.execute(
            "INSERT INTO sources (type, feed_url, display_name, site_url, criteria, threshold,"
            " daily_item_cap, enabled, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(feed_url) DO UPDATE SET display_name = excluded.display_name,"
            " site_url = excluded.site_url, criteria = excluded.criteria,"
            " threshold = excluded.threshold, daily_item_cap = excluded.daily_item_cap,"
            " enabled = excluded.enabled, updated_at = excluded.updated_at",
            (
                source_type,
                feed_url,
                src.get("display_name") or feed_url,
                src.get("site_url"),
                src.get("criteria") or "",
                int(src.get("threshold") or 5),
                int(src.get("daily_item_cap") or (5 if source_type == "youtube" else 10)),
                int(bool(src.get("enabled", True))),
                now,
                now,
            ),
        )
        counts["sources"] += 1
    db.commit()
    return {"imported": counts}
