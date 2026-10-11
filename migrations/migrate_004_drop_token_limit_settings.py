"""delete the settings rows for the retired token limits: triage_max_tokens,
extract_max_tokens and run_token_budget. every call now asks for the
model's own maximum output and there is no run budget, so the rows are
dead config. nothing reads them any more (settings_surface and the config
export walk PUBLIC_SETTINGS only), so this is tidiness rather than a fix --
but a stale row in the table reads like a live knob to anyone poking at the
db by hand. idempotent: deleting absent rows is a no-op.
"""

import os
import sqlite3
from pathlib import Path

RETIRED_KEYS = ("triage_max_tokens", "extract_max_tokens", "run_token_budget")


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
        conn.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in RETIRED_KEYS])
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(
            f"[migrations] 004_drop_token_limit_settings error: {type(e).__name__}: {e}",
            flush=True,
        )
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    sys.exit(0 if migrate() else 1)
