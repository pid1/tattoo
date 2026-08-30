"""ordered migration runner (rally pattern).

each migration file exports migrate() -> bool, is idempotent by
introspection (no version table -- steps re-derive whether they already
ran), and is runnable standalone. ordering is this hand-maintained list.
output format follows the destination: plain lines when stdout is a
terminal (running them by hand, where readability wins), structured json
otherwise -- at container startup these are the first lines in the log
stream, and a viewer should not have to special-case them.

migration 001 creates the schema and therefore must NOT early-return when
the database file is missing; later migrations should return True early if
their target table predates them (rally convention).
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_001_initial import migrate as migrate_001_initial  # noqa: E402
from migrate_002_token_columns import migrate as migrate_002_token_columns  # noqa: E402
from migrate_003_backfill_cutoff import migrate as migrate_003_backfill_cutoff  # noqa: E402

MIGRATIONS = [
    ("001_initial", migrate_001_initial),
    ("002_token_columns", migrate_002_token_columns),
    ("003_backfill_cutoff", migrate_003_backfill_cutoff),
]


def _emit(name: str, ok: bool, error: str | None = None, error_type: str | None = None) -> None:
    if sys.stdout.isatty():
        suffix = "" if ok else f" {error_type}: {error}" if error else ""
        print(f"[migrations] {'ok' if ok else 'FAILED'}: {name}{suffix}", flush=True)
        return
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "level": "info" if ok else "error",
        "subsystem": "migrations",
        "msg": f"{'ok' if ok else 'failed'}: {name}",
        "migration": name,
    }
    if error:
        record["error"] = error
    if error_type:
        record["error_type"] = error_type
    print(json.dumps(record, ensure_ascii=False, default=str), flush=True)


def run_all() -> bool:
    for name, fn in MIGRATIONS:
        try:
            if fn():
                _emit(name, True)
            else:
                _emit(name, False)
                return False
        except Exception as e:
            _emit(name, False, error=str(e), error_type=type(e).__name__)
            return False
    return True


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
