"""next-run arithmetic: the catch-up and no-double-fire rules (plan §0.7)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tattoo.scheduler import next_run, parse_schedule

TZ = ZoneInfo("America/Chicago")


def _at(hour, minute=0):
    return datetime(2026, 8, 6, hour, minute, tzinfo=TZ)


def test_before_slot_runs_today():
    assert next_run(_at(9), "21:00", None) == _at(21)


def test_overdue_unconsumed_slot_runs_now():
    # container restarted at 21:30 before the run happened: catch up immediately
    now = _at(21, 30)
    assert next_run(now, "21:00", "2026-08-05") == now


def test_already_ran_today_waits_for_tomorrow():
    result = next_run(_at(21, 30), "21:00", "2026-08-06")
    assert result == datetime(2026, 8, 7, 21, 0, tzinfo=TZ)


def test_ran_today_but_before_slot_still_skips_to_tomorrow():
    # last_run_date == today consumes the slot no matter the current time
    result = next_run(_at(9), "21:00", "2026-08-06")
    assert result == datetime(2026, 8, 7, 21, 0, tzinfo=TZ)


def test_bad_schedule_string_degrades_to_default():
    assert parse_schedule("not a time") == (21, 0)
    assert parse_schedule("") == (21, 0)
    assert parse_schedule(None) == (21, 0)
    assert parse_schedule("25:99") == (21, 0)


def test_good_schedule_string():
    assert parse_schedule("06:30") == (6, 30)
