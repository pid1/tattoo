"""source lifecycle apis (plan §8). the primary reason the interface
exists: adding a feed must be one paste from the phone, and 'remove' must
default to the reversible disable -- never the destructive purge.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from tattoo import resolver, store
from tattoo.database import get_db

router = APIRouter(tags=["sources"])

DEFAULT_CAPS = {"web": 10, "youtube": 5}

# fields PUT may change; everything else is pipeline-owned state
_EDITABLE = {
    "display_name",
    "site_url",
    "criteria",
    "threshold",
    "daily_item_cap",
    "enabled",
    "options",
}


def _row_to_dict(db, row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "feed_url": row["feed_url"],
        "display_name": row["display_name"],
        "site_url": row["site_url"],
        "criteria": row["criteria"],
        "threshold": row["threshold"],
        "daily_item_cap": row["daily_item_cap"],
        "options": json.loads(row["options"] or "{}"),
        "enabled": bool(row["enabled"]),
        "last_polled_at": row["last_polled_at"],
        "last_poll_status": row["last_poll_status"],
        "stats": store.source_stats(db, row["id"]),
    }


@router.get("/api/sources")
def list_sources(db: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = db.execute("SELECT * FROM sources ORDER BY display_name").fetchall()
    return {"sources": [_row_to_dict(db, r) for r in rows]}


@router.post("/api/sources/resolve")
def resolve_source(body: dict, db: sqlite3.Connection = Depends(get_db)) -> dict:
    text = (body.get("input") or "").strip()
    try:
        return resolver.resolve(text, store.get_secret(db, "youtube_api_key"))
    except resolver.ResolveError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except RuntimeError as e:
        # normalized http errors from the fetch layer
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/api/sources", status_code=201)
def create_source(body: dict, db: sqlite3.Connection = Depends(get_db)) -> dict:
    source_type = body.get("type")
    feed_url = (body.get("feed_url") or "").strip()
    display_name = (body.get("display_name") or "").strip()
    if source_type not in ("web", "youtube") or not feed_url or not display_name:
        raise HTTPException(
            status_code=422,
            detail="type (web|youtube), feed_url, and display_name are required",
        )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        cur = db.execute(
            "INSERT INTO sources (type, feed_url, display_name, site_url, criteria, threshold,"
            " daily_item_cap, options, enabled, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                source_type,
                feed_url,
                display_name,
                (body.get("site_url") or "").strip() or None,
                body.get("criteria") or "",
                int(body.get("threshold") or 5),
                int(body.get("daily_item_cap") or DEFAULT_CAPS[source_type]),
                json.dumps(body.get("options") or {}),
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409, detail="a source with that feed url exists"
        ) from e
    db.commit()
    row = db.execute("SELECT * FROM sources WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_dict(db, row)


@router.get("/api/sources/{source_id}")
def get_source(source_id: int, db: sqlite3.Connection = Depends(get_db)) -> dict:
    row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    return _row_to_dict(db, row)


@router.put("/api/sources/{source_id}")
def update_source(
    source_id: int, body: dict, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    unknown = set(body) - _EDITABLE
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"not editable: {', '.join(sorted(unknown))}"
        )

    assignments, values = [], []
    for key, value in body.items():
        if key in ("threshold", "daily_item_cap"):
            value = int(value)
        elif key == "enabled":
            value = int(bool(value))  # disable = the reversible remove (plan §8)
        elif key == "options":
            value = json.dumps(value or {})
        assignments.append(f"{key} = ?")
        values.append(value)
    assignments.append("updated_at = ?")
    values.append(datetime.now(UTC).isoformat(timespec="seconds"))
    values.append(source_id)
    db.execute(f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?", values)
    db.commit()
    row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _row_to_dict(db, row)


@router.delete("/api/sources/{source_id}", status_code=204)
def purge_source(source_id: int, db: sqlite3.Connection = Depends(get_db)) -> None:
    """the destructive path: deletes the source and every associated row via
    the ON DELETE CASCADE chain. the ui gates this behind a confirmation
    that warns cached content cannot be refetched."""
    row = db.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="source not found")
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    db.commit()
