"""test harness. the real database and dist directory are never touched:
every test gets tmp paths via env vars, which config reads lazily. the
client fixture builds a fresh app via create_app() against those paths and
deliberately does not run lifespan, so the scheduler thread never starts
in tests (rally's convention, same reason).
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TATTOO_DB_PATH", str(tmp_path / "data" / "tattoo.db"))
    monkeypatch.setenv("TATTOO_DIST_PATH", str(tmp_path / "dist"))
    monkeypatch.setenv("TATTOO_BACKUP_KEEP", "3")
    # secrets must come from the test, never the developer's shell
    for var in ("ANTHROPIC_API_KEY", "PUSHOVER_API_KEY", "PUSHOVER_USER_KEY", "YOUTUBE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def db(isolated_env):
    """initialized database (runs the real migrations) plus an open connection."""
    from tattoo import database

    database.init_db()
    conn = database.connect()
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    """TestClient over a fresh app. not a context manager on purpose:
    lifespan (scheduler thread) must not run in tests."""
    from fastapi.testclient import TestClient

    from tattoo.main import create_app

    return TestClient(app=create_app())
