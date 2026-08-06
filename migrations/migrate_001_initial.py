"""initial schema (plan §4). idempotent: every statement is IF NOT EXISTS.

this migration is the schema creator, so unlike later migrations it must
run when the database file does not exist yet.
"""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id               INTEGER PRIMARY KEY,
    type             TEXT NOT NULL CHECK (type IN ('web','youtube')),
    feed_url         TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL,
    site_url         TEXT,
    criteria         TEXT NOT NULL DEFAULT '',
    threshold        INTEGER NOT NULL DEFAULT 5,
    daily_item_cap   INTEGER NOT NULL,
    options          TEXT NOT NULL DEFAULT '{}',
    enabled          INTEGER NOT NULL DEFAULT 1,
    etag             TEXT,
    last_modified    TEXT,
    last_polled_at   TEXT,
    last_poll_status TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id      TEXT NOT NULL,
    canonical_url    TEXT NOT NULL,
    title            TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    author           TEXT,
    published_at     TEXT,
    first_seen_at    TEXT NOT NULL,
    enrich_meta      TEXT NOT NULL DEFAULT '{}',
    degraded         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_items_source_id ON items(source_id);
CREATE INDEX IF NOT EXISTS ix_items_canonical_url ON items(canonical_url);

CREATE TABLE IF NOT EXISTS content (
    item_id    INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    method     TEXT NOT NULL CHECK (method IN ('feed_body','extracted','transcript','summary_fallback')),
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    items_seen   INTEGER NOT NULL DEFAULT 0,
    items_judged INTEGER NOT NULL DEFAULT 0,
    items_passed INTEGER NOT NULL DEFAULT 0,
    token_usage  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS prompt_history (
    id           INTEGER PRIMARY KEY,
    field_name   TEXT NOT NULL,
    value        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prompt_history_field_name ON prompt_history(field_name);

CREATE TABLE IF NOT EXISTS judgments (
    id                INTEGER PRIMARY KEY,
    item_id           INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    prompt_history_id INTEGER NOT NULL REFERENCES prompt_history(id),
    score             INTEGER NOT NULL,
    justification     TEXT NOT NULL,
    passed            INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_judgments_item_id ON judgments(item_id);

CREATE TABLE IF NOT EXISTS extractions (
    id                INTEGER PRIMARY KEY,
    item_id           INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    run_id            INTEGER NOT NULL REFERENCES runs(id),
    prompt_history_id INTEGER NOT NULL REFERENCES prompt_history(id),
    bluf              TEXT NOT NULL,
    not_answered      TEXT NOT NULL DEFAULT '',
    specifics         TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_extractions_item_id ON extractions(item_id);

CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY,
    extraction_id INTEGER NOT NULL REFERENCES extractions(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    locator       TEXT
);
CREATE INDEX IF NOT EXISTS ix_findings_extraction_id ON findings(extraction_id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _db_path() -> str:
    path = os.environ.get("TATTOO_DB_PATH")
    if path:
        return path
    prod = Path("/data/tattoo.db")
    dev = Path(__file__).resolve().parent.parent / "data" / "tattoo.db"
    return str(prod) if prod.exists() else str(dev)


def migrate() -> bool:
    path = Path(_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"[migrations] 001_initial error: {type(e).__name__}: {e}", flush=True)
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    sys.exit(0 if migrate() else 1)
