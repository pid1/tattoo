"""settings and secret resolution: db roundtrip, defaults, and the
env-override-wins rule for secrets."""

from tattoo import store


def test_setting_roundtrip(db):
    store.set_setting(db, "schedule_time", "06:30")
    assert store.get_setting(db, "schedule_time") == "06:30"


def test_setting_upsert_overwrites(db):
    store.set_setting(db, "k", "v1")
    store.set_setting(db, "k", "v2")
    assert store.get_setting(db, "k") == "v2"
    assert (
        db.execute("SELECT COUNT(*) AS n FROM settings WHERE key = 'k'").fetchone()["n"]
        == 1
    )


def test_defaults_apply_without_rows(db):
    assert store.get_setting(db, "schedule_time") == "21:00"
    assert store.get_setting(db, "shadow_mode") == "true"
    assert store.get_setting(db, "nonexistent", "fallback") == "fallback"


def test_secret_from_db(db):
    store.set_setting(db, "pushover_api_key", "db-token")
    assert store.get_secret(db, "pushover_api_key") == "db-token"


def test_secret_env_override_wins(db, monkeypatch):
    store.set_setting(db, "pushover_api_key", "db-token")
    monkeypatch.setenv("PUSHOVER_API_KEY", "env-token")
    assert store.get_secret(db, "pushover_api_key") == "env-token"


def test_secret_empty_env_falls_through_to_db(db, monkeypatch):
    store.set_setting(db, "pushover_api_key", "db-token")
    monkeypatch.setenv("PUSHOVER_API_KEY", "   ")
    assert store.get_secret(db, "pushover_api_key") == "db-token"


def test_secret_unset_everywhere_is_none(db):
    assert store.get_secret(db, "anthropic_api_key") is None


def test_local_tz_from_settings(db):
    store.set_setting(db, "timezone", "America/New_York")
    assert str(store.local_tz(db)) == "America/New_York"


def test_local_tz_bad_value_degrades(db):
    store.set_setting(db, "timezone", "Not/AZone")
    assert store.local_tz(db) is not None  # falls back, never raises
