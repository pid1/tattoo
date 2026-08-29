"""add sources.backfill_cutoff: the published-at boundary a source's first
poll establishes, so a newly added feed contributes its newest few items
instead of its whole back catalogue.

NULL means "never successfully polled" -- the pipeline treats that as the
first run, keeps only the newest FIRST_RUN_ITEM_LIMIT entries, and stamps
the cutoff from them. every later poll ignores anything published before
it, which is what stops a deep archive from dripping through the daily cap
one day's worth at a time.

sources that already exist when this runs have been ingesting history for
days; they get a cutoff at the newest item they have already seen (falling
back to the migration timestamp for a source with no dated items), which
ends the drip immediately without re-judging anything. that backfill runs
only in the branch that adds the column, so a re-run never stamps a source
still waiting for its first poll. idempotent by column introspection.
"""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _db_path() -> str:
    path = os.environ.get("TATTOO_DB_PATH")
    if path:
        return path
    prod = Path("/data/tattoo.db")
    dev = Path(__file__).resolve().parent.parent / "data" / "tattoo.db"
    return str(prod) if prod.exists() else str(dev)


def migrate() -> bool:
    path = Path(_db_path())
    if not path.exists():
        return True  # 001 creates the file; standalone against nothing is a no-op

    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "backfill_cutoff" not in columns:
            conn.execute("ALTER TABLE sources ADD COLUMN backfill_cutoff TEXT")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE sources SET backfill_cutoff = COALESCE("
                " (SELECT MAX(i.published_at) FROM items i WHERE i.source_id = sources.id), ?)",
                (now,),
            )
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(
            f"[migrations] 003_backfill_cutoff error: {type(e).__name__}: {e}",
            flush=True,
        )
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    sys.exit(0 if migrate() else 1)
