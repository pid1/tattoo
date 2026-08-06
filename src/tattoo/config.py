"""env-var configuration.

paths and knobs come from env with dev-friendly defaults; runtime
configuration (schedule, models, thresholds) is authoritative in the
settings table (store.py), and secrets resolve env-first there too.
everything here is read lazily via functions so tests can repoint with
monkeypatch.setenv without import-order games.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# src/tattoo/config.py -> repo root. templates/, static/, migrations/ live
# at the root, outside the package, and are copied separately in the
# Dockerfile (rally convention).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

FALLBACK_TIMEZONE = "America/Chicago"


def db_path() -> Path:
    return Path(os.environ.get("TATTOO_DB_PATH", REPO_ROOT / "data" / "tattoo.db"))


def dist_path() -> Path:
    return Path(os.environ.get("TATTOO_DIST_PATH", REPO_ROOT / "dist"))


def backup_keep() -> int:
    raw = os.environ.get("TATTOO_BACKUP_KEEP", "10")
    try:
        return int(raw)
    except ValueError:
        return 10


def default_timezone() -> ZoneInfo:
    """fallback chain when the settings table has no timezone yet: TZ env,
    then a hard default. store.local_tz() consults the db first."""
    name = os.environ.get("TZ", "").strip() or FALLBACK_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(FALLBACK_TIMEZONE)


def ensure_dirs() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    (db_path().parent / "backups").mkdir(parents=True, exist_ok=True)
    for sub in ("dashboard", "archive"):
        (dist_path() / sub).mkdir(parents=True, exist_ok=True)
