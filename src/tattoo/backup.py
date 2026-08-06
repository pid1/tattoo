"""sqlite snapshots (puffin's backup.py, ported).

uses sqlite's online backup api, which yields a consistent copy under
concurrent writes. snapshots land in <db-dir>/backups/ with fixed-width
utc stamps so lexical sort == chronological sort, which pruning relies on.
never raises: a failed backup logs and returns None -- it must not block
startup. invoked automatically with reason="pre-migration" from
database.init_db(), and on demand via `python -m tattoo.backup`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tattoo.log import log


def backup_database(
    db_path: Path | str, *, reason: str = "manual", keep: int | None = None
) -> Path | None:
    src_path = Path(db_path)
    if not src_path.exists() or src_path.stat().st_size == 0:
        return None  # fresh install, nothing to snapshot

    backups = src_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    # microseconds in the stamp so a manual backup immediately after a
    # pre-migration one doesn't collide
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    dest = backups / f"{src_path.stem}-{stamp}-{reason}.db"

    src = dst = None
    try:
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
    except sqlite3.Error as e:
        log("backup", f"backup failed: {type(e).__name__}: {e}", level="error")
        dest.unlink(missing_ok=True)
        return None
    finally:
        if src is not None:
            src.close()
        if dst is not None:
            dst.close()

    _prune(backups, src_path.stem, keep)
    log("backup", "snapshot written", path=str(dest), reason=reason)
    return dest


def _prune(backups: Path, stem: str, keep: int | None) -> None:
    if keep is None:
        from tattoo import config  # local import: keep module usable standalone

        keep = config.backup_keep()
    if keep <= 0:
        return  # pruning disabled
    snapshots = sorted(backups.glob(f"{stem}-*.db"))
    for old in snapshots[:-keep]:
        old.unlink(missing_ok=True)


def main() -> None:
    from tattoo import config

    path = backup_database(config.db_path(), reason="manual")
    print(path if path else "no backup taken (missing or empty database)")


if __name__ == "__main__":
    main()
