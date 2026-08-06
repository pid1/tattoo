"""ordered migration runner (rally pattern).

each migration file exports migrate() -> bool, is idempotent by
introspection (no version table -- steps re-derive whether they already
ran), and is runnable standalone. ordering is this hand-maintained list.
migrations print plain lines rather than structured json: they run once
at startup and standalone from the shell, where readability wins.

migration 001 creates the schema and therefore must NOT early-return when
the database file is missing; later migrations should return True early if
their target table predates them (rally convention).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_001_initial import migrate as migrate_001_initial  # noqa: E402
from migrate_002_token_columns import migrate as migrate_002_token_columns  # noqa: E402

MIGRATIONS = [
    ("001_initial", migrate_001_initial),
    ("002_token_columns", migrate_002_token_columns),
]


def run_all() -> bool:
    for name, fn in MIGRATIONS:
        try:
            if fn():
                print(f"[migrations] ok: {name}", flush=True)
            else:
                print(f"[migrations] FAILED: {name}", flush=True)
                return False
        except Exception as e:
            print(f"[migrations] FAILED: {name} raised {type(e).__name__}: {e}", flush=True)
            return False
    return True


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
