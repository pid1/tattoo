"""add per-call token columns to judgments and extractions, so the source
list can show tokens consumed per source over 30 days (plan §8) without
guessing from run-level totals. idempotent by column introspection.
"""

import os
import sqlite3
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
        # standalone invocation against a missing db: nothing to alter.
        # under run_migrations.py this never happens -- 001 runs first and
        # creates the file.
        return True

    conn = sqlite3.connect(path)
    try:
        for table in ("judgments", "extractions"):
            # `table` and `column` below both come from the literal tuples in this
            # loop; identifiers cannot be bound parameters in SQLite DDL.
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column in ("input_tokens", "output_tokens"):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"[migrations] 002_token_columns error: {type(e).__name__}: {e}", flush=True)
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    sys.exit(0 if migrate() else 1)
