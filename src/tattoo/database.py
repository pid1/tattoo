"""sqlite access. stdlib sqlite3 by decision (plan §0.2).

connection factory with row factory and the per-connection foreign-key
pragma (sqlite defaults it off, so ON DELETE clauses are otherwise inert --
puffin convention), a fastapi dependency, and the startup sequence:
pre-migration backup -> ordered idempotent migrations (rally-style files
under migrations/, run in-process the way puffin does).
"""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from tattoo import config
from tattoo.log import log


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: fastapi may run a sync dependency generator
    # and its endpoint on different threadpool threads. safe here because
    # every request/thread gets its own connection and never shares it.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """startup: ensure directories, snapshot the db, apply migrations.
    migration failure is one of the few things that legitimately fails
    startup -- serving against a half-migrated schema is worse."""
    config.ensure_dirs()

    from tattoo import backup  # local import on purpose: avoids a circular import

    backup.backup_database(config.db_path(), reason="pre-migration")

    runner = _load_migration_runner()
    if not runner.run_all():
        raise RuntimeError("database migrations failed; refusing to start")
    log("database", "migrations complete", db=str(config.db_path()))


def _load_migration_runner():
    """load migrations/run_migrations.py by path. migrations live at the repo
    root outside the package (rally convention), so they are not importable
    by package name."""
    path = config.REPO_ROOT / "migrations" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("tattoo_migration_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
