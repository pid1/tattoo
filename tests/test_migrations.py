"""migrations must be idempotent and produce the full schema, and the
per-connection foreign-key pragma must actually enforce references --
sqlite silently ignores ON DELETE clauses without it."""

import sqlite3

import pytest

from tattoo import config, database

EXPECTED_TABLES = {
    "sources",
    "items",
    "content",
    "runs",
    "judgments",
    "extractions",
    "findings",
    "settings",
    "prompt_history",
}


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r["name"] for r in rows}


def test_init_creates_schema(db):
    assert EXPECTED_TABLES <= _tables(db)


def test_init_is_idempotent():
    database.init_db()
    database.init_db()  # second run must be a clean no-op
    conn = database.connect()
    try:
        assert EXPECTED_TABLES <= _tables(conn)
    finally:
        conn.close()


def test_pre_migration_backup_taken_on_existing_db():
    database.init_db()
    conn = database.connect()
    conn.execute("INSERT INTO settings (key, value, updated_at) VALUES ('x', 'y', 'now')")
    conn.commit()
    conn.close()

    database.init_db()  # existing non-empty db -> snapshot before migrating
    backups = list((config.db_path().parent / "backups").glob("*-pre-migration.db"))
    assert len(backups) == 1


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO items (source_id, external_id, canonical_url, title,"
            " normalized_title, first_seen_at) VALUES (999, 'x', 'u', 't', 't', 'now')"
        )


def test_cascade_delete_source_removes_items(db):
    db.execute(
        "INSERT INTO sources (type, feed_url, display_name, daily_item_cap, created_at, updated_at)"
        " VALUES ('web', 'https://example.com/feed', 'example', 10, 'now', 'now')"
    )
    source_id = db.execute("SELECT id FROM sources").fetchone()["id"]
    db.execute(
        "INSERT INTO items (source_id, external_id, canonical_url, title,"
        " normalized_title, first_seen_at) VALUES (?, 'x', 'u', 't', 't', 'now')",
        (source_id,),
    )
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


def _load_migration(name):
    """migrations live outside the package and are not importable by name
    (database._load_migration_runner has the same problem)."""
    import importlib.util

    path = config.REPO_ROOT / "migrations" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate


def test_backfill_cutoff_stamped_for_pre_existing_sources():
    """003 runs against a database that has been ingesting history for days:
    each existing source is pinned at the newest item it already has, so the
    archive stops dripping in without re-judging anything. a source added
    afterwards keeps a NULL cutoff -- its first poll has not happened yet."""
    migrate_003 = _load_migration("migrate_003_backfill_cutoff")

    database.init_db()
    conn = database.connect()
    conn.execute(
        "INSERT INTO sources (type, feed_url, display_name, daily_item_cap, created_at, updated_at)"
        " VALUES ('web', 'https://example.com/feed', 'example', 10, 'now', 'now')"
    )
    source_id = conn.execute("SELECT id FROM sources").fetchone()["id"]
    for external_id, published in (("a", "2026-08-01T00:00:00+00:00"), ("b", None)):
        conn.execute(
            "INSERT INTO items (source_id, external_id, canonical_url, title, normalized_title,"
            " published_at, first_seen_at) VALUES (?, ?, 'u', 't', 't', ?, 'now')",
            (source_id, external_id, published),
        )
    # simulate the pre-003 shape: drop the column the migration adds
    conn.execute("ALTER TABLE sources DROP COLUMN backfill_cutoff")
    conn.commit()
    conn.close()

    assert migrate_003() is True
    assert migrate_003() is True  # idempotent: second run must not re-stamp

    conn = database.connect()
    try:
        row = conn.execute("SELECT * FROM sources").fetchone()
        assert row["backfill_cutoff"] == "2026-08-01T00:00:00+00:00"

        conn.execute(
            "INSERT INTO sources (type, feed_url, display_name, daily_item_cap,"
            " created_at, updated_at)"
            " VALUES ('web', 'https://later.example/feed', 'later', 10, 'now', 'now')"
        )
        conn.commit()
        assert migrate_003() is True
        later = conn.execute(
            "SELECT backfill_cutoff FROM sources WHERE display_name = 'later'"
        ).fetchone()
        assert later["backfill_cutoff"] is None
    finally:
        conn.close()


def test_connection_usable_across_threads(db):
    """fastapi runs the get_db generator and the endpoint on different
    threadpool threads; without check_same_thread=False every settings-page
    load 500'd under its parallel fetches (found by the docker e2e)."""
    import threading

    errors = []

    def use_connection():
        try:
            db.execute("SELECT 1").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    thread = threading.Thread(target=use_connection)
    thread.start()
    thread.join()
    assert errors == []
