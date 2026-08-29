"""settings-table access and secret resolution.

configuration is authoritative in sqlite (plan §8) — rally's key/value
settings shape. secrets resolve env-first: an env var override wins over
the db and is never written back, so there is a path that never touches
appdata.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tattoo import config

# settings keys that hold secrets, and the env var that overrides each
SECRET_ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "pushover_api_key": "PUSHOVER_API_KEY",
    "pushover_user_key": "PUSHOVER_USER_KEY",
    "youtube_api_key": "YOUTUBE_API_KEY",
}

# defaults applied when a key has no row yet; values are strings because
# everything in the settings table is a string (rally convention)
DEFAULTS = {
    "schedule_time": "21:00",
    "retention_days": "90",
    "triage_max_tokens": "1000",
    "extract_max_tokens": "2000",
    "shadow_mode": "true",
    "triage_model": "claude-haiku-4-5",
    "extract_model": "claude-sonnet-5",
}


def get_setting(
    conn: sqlite3.Connection, key: str, default: str | None = None
) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULTS.get(key, default)


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), now),
    )
    conn.commit()


def get_secret(conn: sqlite3.Connection, key: str) -> str | None:
    """env override wins over the db; empty strings count as unset."""
    env_name = SECRET_ENV.get(key)
    if env_name:
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
    val = get_setting(conn, key)
    val = (val or "").strip()
    return val or None


def local_tz(conn: sqlite3.Connection) -> ZoneInfo:
    """settings-table timezone wins; fall back to the env chain."""
    name = (get_setting(conn, "timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass  # bad value in the db degrades to the fallback, never crashes a run
    return config.default_timezone()


# settings keys the ui may read and write; everything else in the table is
# internal state (pointers, last_run_date) and stays off the wire
PUBLIC_SETTINGS = (
    "schedule_time",
    "timezone",
    "page_url",
    "triage_model",
    "extract_model",
    "triage_max_tokens",
    "extract_max_tokens",
    "run_token_budget",
    "retention_days",
    "shadow_mode",
)


def settings_surface(conn: sqlite3.Connection) -> dict:
    """what GET /api/settings returns: public settings verbatim, secrets as
    set/hint/env-override metadata only -- the value itself never goes back
    to the page (plan §0.4)."""
    settings = {k: get_setting(conn, k, "") or "" for k in PUBLIC_SETTINGS}
    secrets = {}
    for key, env_name in SECRET_ENV.items():
        effective = get_secret(conn, key)
        secrets[key] = {
            "set": bool(effective),
            "hint": f"…{effective[-4:]}" if effective else "",
            "env_override": bool(os.environ.get(env_name, "").strip()),
        }
    return {"settings": settings, "secrets": secrets}


def apply_settings(conn: sqlite3.Connection, updates: dict) -> list[str]:
    """bulk upsert from the ui. blank secret values mean 'keep existing'
    (the write-only field convention); unknown keys are rejected by name."""
    unknown = [k for k in updates if k not in PUBLIC_SETTINGS and k not in SECRET_ENV]
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))
    applied = []
    for key, value in updates.items():
        value = "" if value is None else str(value)
        if key in SECRET_ENV and not value.strip():
            continue  # never blank a stored secret from an empty form field
        set_setting(conn, key, value)
        applied.append(key)
    return applied


def source_stats(conn: sqlite3.Connection, source_id: int) -> dict:
    """the numbers the remove decision needs (plan §8): pass rate says the
    source stopped being worth the tokens; token spend says which one is
    quietly eating the budget."""
    from datetime import timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    items_total = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id = ?", (source_id,)
    ).fetchone()["n"]
    judged = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(j.passed), 0) AS passed,"
        " COALESCE(SUM(j.input_tokens + j.output_tokens), 0) AS tokens"
        " FROM judgments j JOIN items i ON i.id = j.item_id"
        " WHERE i.source_id = ? AND j.created_at >= ?",
        (source_id, cutoff),
    ).fetchone()
    extract_tokens = conn.execute(
        "SELECT COALESCE(SUM(e.input_tokens + e.output_tokens), 0) AS tokens"
        " FROM extractions e JOIN items i ON i.id = e.item_id"
        " WHERE i.source_id = ? AND e.created_at >= ?",
        (source_id, cutoff),
    ).fetchone()["tokens"]
    last_passing = conn.execute(
        "SELECT MAX(j.created_at) AS at FROM judgments j JOIN items i ON i.id = j.item_id"
        " WHERE i.source_id = ? AND j.passed = 1",
        (source_id,),
    ).fetchone()["at"]
    return {
        "items_total": items_total,
        "judged_30d": judged["n"],
        "pass_rate_30d": (
            round(judged["passed"] / judged["n"], 2) if judged["n"] else None
        ),
        "last_passing_at": last_passing,
        "tokens_30d": judged["tokens"] + extract_tokens,
    }
