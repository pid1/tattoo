"""structured json logging to stdout for the alloy/loki pipeline.

one helper, no logging-module ceremony. every event carries ts, level,
subsystem, and msg; extra fields ride along as top-level json keys. the
subsystem field plays the role of reveille's [build]/[pushover] prefixes,
and every skip path logs a reason so silence is never ambiguous. flush on
every line so a buffered stdout never hides the last event.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime


def log(subsystem: str, msg: str, level: str = "info", **fields) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "level": level,
        "subsystem": subsystem,
        "msg": msg,
    }
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False, default=str), flush=True)
