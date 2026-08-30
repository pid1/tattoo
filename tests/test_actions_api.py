"""actions api: run-now wakes the scheduler, reprocess runs in the
background, export excludes secrets, import round-trips."""

import threading

from tattoo import store
from tattoo.scheduler import default_scheduler


def test_run_now_triggers_scheduler(client):
    default_scheduler._wake.clear()
    resp = client.post("/api/run")
    assert resp.status_code == 202
    assert default_scheduler._wake.is_set()
    default_scheduler._wake.clear()


def test_reprocess_runs_in_background(client, monkeypatch):
    done = threading.Event()
    monkeypatch.setattr("tattoo.pipeline.reprocess", lambda: done.set())
    resp = client.post("/api/reprocess")
    assert resp.status_code == 202
    assert done.wait(timeout=5)


def test_export_excludes_secrets_and_roundtrips(client, db):
    store.set_setting(db, "anthropic_api_key", "sk-super-secret")
    store.set_setting(db, "schedule_time", "06:15")
    client.post(
        "/api/sources",
        json={
            "type": "web",
            "feed_url": "https://example.com/feed.xml",
            "display_name": "Example",
            "criteria": 'multi\nline "criteria"',
        },
    )

    exported = client.get("/api/export/config").text
    assert "sk-super-secret" not in exported
    assert 'schedule_time = "06:15"' in exported
    assert "[[sources]]" in exported

    # wipe and re-import into the same db
    db.execute("DELETE FROM sources")
    db.commit()
    store.set_setting(db, "schedule_time", "21:00")

    result = client.post("/api/import/config", json={"toml": exported}).json()
    assert result["imported"]["sources"] == 1
    assert store.get_setting(db, "schedule_time") == "06:15"
    row = db.execute("SELECT * FROM sources").fetchone()
    assert row["criteria"] == 'multi\nline "criteria"'
    # secrets untouched by import
    assert store.get_secret(db, "anthropic_api_key") == "sk-super-secret"


def test_import_is_idempotent(client):
    client.post(
        "/api/sources",
        json={"type": "web", "feed_url": "https://e/f", "display_name": "E"},
    )
    exported = client.get("/api/export/config").text
    client.post("/api/import/config", json={"toml": exported})
    client.post("/api/import/config", json={"toml": exported})
    assert len(client.get("/api/sources").json()["sources"]) == 1


def test_import_bad_toml_422(client):
    assert client.post("/api/import/config", json={"toml": "not [ toml"}).status_code == 422
