"""backup contract (puffin's): consistent snapshots, sortable names,
retention pruning, and never-raises on failure."""

import sqlite3

from tattoo import backup, config, database


def _make_db():
    database.init_db()
    return config.db_path()


def test_backup_missing_db_is_noop(isolated_env):
    assert backup.backup_database(config.db_path()) is None


def test_backup_creates_snapshot(isolated_env):
    path = _make_db()
    dest = backup.backup_database(path, reason="manual")
    assert dest is not None and dest.exists()
    assert dest.name.startswith("tattoo-") and dest.name.endswith("-manual.db")
    # snapshot must be a readable sqlite db with the schema
    conn = sqlite3.connect(dest)
    try:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "settings" in names


def test_prune_keeps_newest(isolated_env):
    path = _make_db()
    for _ in range(5):
        backup.backup_database(path, keep=3)
    snapshots = sorted((path.parent / "backups").glob("tattoo-*.db"))
    assert len(snapshots) == 3


def test_backup_failure_returns_none_without_raising(isolated_env, monkeypatch):
    path = _make_db()

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(sqlite3, "connect", boom)
    assert backup.backup_database(path) is None
