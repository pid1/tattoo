"""in-process scheduler (plan §0.7).

a daemon thread that re-reads its schedule from the db each cycle, so
schedule and timezone changes apply without a restart. a threading.Event
lets POST /api/run (M6) wake it immediately. the last-run date persists in
settings so a restart during the run hour does not double-fire -- rally's
in-memory LAST_RUN_DATE had exactly that bug.

manual runs do not consume the daily slot: the scheduled run still fires
at its time even if a manual run happened earlier the same day.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from tattoo.log import log

# re-read settings at least this often even when idle, so a schedule change
# never waits more than an hour to take effect
_MAX_WAIT_S = 3600.0

_DEFAULT_SCHEDULE = (21, 0)


def parse_schedule(value: str | None) -> tuple[int, int]:
    """tolerant HH:MM parse; anything malformed degrades to the default
    rather than crashing the loop."""
    try:
        hh, mm = (value or "").strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except ValueError, AttributeError:
        pass
    return _DEFAULT_SCHEDULE


def next_run(now: datetime, schedule_time: str | None, last_run_date: str | None) -> datetime:
    """next scheduled instant. if today's slot is overdue and unconsumed,
    that is *now* (catch-up after a restart); if today already ran, tomorrow."""
    hh, mm = parse_schedule(schedule_time)
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    if last_run_date == today:
        return candidate + timedelta(days=1)
    if candidate <= now:
        return now
    return candidate


def _run_pipeline(reason: str) -> None:
    # lazy import: scheduler must be importable without dragging the pipeline
    from tattoo import pipeline

    pipeline.run(reason)


class Scheduler:
    def __init__(self, run_fn: Callable[[str], None]):
        self._run_fn = run_fn
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tattoo-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        """wake the loop for an immediate manual run."""
        self._wake.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        # local imports: the thread must not import the web app
        from tattoo import database, store

        while not self._stop.is_set():
            conn = database.connect()
            try:
                tz = store.local_tz(conn)
                schedule = store.get_setting(conn, "schedule_time")
                last_run = store.get_setting(conn, "last_run_date")
            finally:
                conn.close()

            now = datetime.now(tz)
            target = next_run(now, schedule, last_run)
            wait_s = max(0.0, (target - now).total_seconds())
            log(
                "scheduler",
                "waiting",
                next_run=target.isoformat(timespec="seconds"),
                wait_seconds=round(wait_s),
            )

            woke = self._wake.wait(timeout=min(wait_s, _MAX_WAIT_S)) if wait_s > 0 else False
            self._wake.clear()
            if self._stop.is_set():
                break

            now = datetime.now(tz)
            if not woke and now < target:
                continue  # hourly settings re-read; not time yet

            reason = "manual" if woke else "scheduled"
            try:
                self._run_fn(reason)
            except Exception as e:
                # the pipeline has its own error handling; this catch only
                # keeps a bug there from killing the scheduler thread
                log("scheduler", f"run crashed: {type(e).__name__}: {e}", level="error")
            finally:
                if reason == "scheduled":
                    # record the attempt regardless of outcome: failures are
                    # surfaced by push/log, and retrying hourly all night on a
                    # persistent failure would be worse
                    conn = database.connect()
                    try:
                        store.set_setting(conn, "last_run_date", now.strftime("%Y-%m-%d"))
                    finally:
                        conn.close()


# module-level singleton so the web app and the actions router share one
# instance without importing each other (avoids a main<->router cycle)
default_scheduler = Scheduler(_run_pipeline)
