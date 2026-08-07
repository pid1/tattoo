"""pipeline contracts: runs open and close as data, dedupe holds across
re-runs, caps count the whole local day, a failing source never fails the
run, content is acquired and cached, the gate wires through with shadow
semantics, and quiet days skip the push."""

import json
from datetime import UTC, datetime, timedelta

from tattoo import config, judge, pipeline, render, store

LONG_BODY = "<p>" + ("substantive words here. " * 40) + "</p>"


def _seed_source(db, cap=10, name="example", feed="https://example.com/feed.xml", threshold=5):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO sources (type, feed_url, display_name, daily_item_cap, threshold,"
        " created_at, updated_at) VALUES ('web', ?, ?, ?, ?, ?, ?)",
        (feed, name, cap, threshold, now, now),
    )
    db.commit()


def _entries(n, prefix="item"):
    # feed body included so the acquire stage takes the zero-network
    # feed_body path in tests
    return [
        {
            "external_id": f"{prefix}-{i}",
            "url": f"https://example.com/{prefix}-{i}",
            "title": f"{prefix} {i}",
            "author": None,
            "published_at": f"2026-08-06T{i:02d}:00:00+00:00",
            "feed_body_html": LONG_BODY,
            "summary_html": "<p>summary.</p>",
        }
        for i in range(n)
    ]


def _patch_poll(monkeypatch, entries, etag="e1"):
    monkeypatch.setattr(
        pipeline.web,
        "poll",
        lambda src: {
            "entries": entries,
            "etag": etag,
            "last_modified": None,
            "not_modified": False,
        },
    )


# -- M0 contracts, still holding ------------------------------------------


def test_run_writes_pages_and_closes_run(db):
    pipeline.run("manual")

    row = db.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "ok"
    assert row["finished_at"] is not None
    assert (config.dist_path() / "dashboard" / "index.html").exists()


def test_run_failure_recorded_as_data(db, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(render, "write_pages", boom)
    pipeline.run("manual")  # must not raise

    row = db.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "failed"


# -- M1: poll, diff, caps ---------------------------------------------------


def test_poll_ingests_and_dedupes_across_runs(db, monkeypatch):
    _seed_source(db)
    _patch_poll(monkeypatch, _entries(3))

    pipeline.run("manual")
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 3

    pipeline.run("manual")  # same entries again: no duplicates
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 3

    src = db.execute("SELECT * FROM sources").fetchone()
    assert src["etag"] == "e1"
    assert src["last_poll_status"] == "ok"

    run_rows = db.execute("SELECT items_seen FROM runs ORDER BY id").fetchall()
    assert [r["items_seen"] for r in run_rows] == [3, 0]


def test_daily_cap_counts_whole_day_not_per_run(db, monkeypatch, capsys):
    _seed_source(db, cap=4)
    _patch_poll(monkeypatch, _entries(3))
    pipeline.run("manual")

    _patch_poll(monkeypatch, _entries(3, prefix="later"))
    pipeline.run("manual")  # only 1 slot left in today's budget

    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 4
    logged = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"daily cap reached' in line
    ]
    assert logged and logged[-1]["dropped"] == 2


def test_cap_keeps_newest_items(db, monkeypatch):
    _seed_source(db, cap=2)
    _patch_poll(monkeypatch, _entries(5))
    pipeline.run("manual")

    titles = {r["title"] for r in db.execute("SELECT title FROM items")}
    assert titles == {"item 4", "item 3"}


def test_failing_source_never_fails_run(db, monkeypatch):
    _seed_source(db, name="good", feed="https://good.example/feed")
    _seed_source(db, name="bad", feed="https://bad.example/feed")

    def poll(src):
        if src["display_name"] == "bad":
            raise RuntimeError("HTTP 500 from https://bad.example/feed: boom")
        return {"entries": _entries(2), "etag": None, "last_modified": None, "not_modified": False}

    monkeypatch.setattr(pipeline.web, "poll", poll)
    pipeline.run("manual")

    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "ok"
    bad = db.execute("SELECT * FROM sources WHERE display_name = 'bad'").fetchone()
    assert "HTTP 500" in bad["last_poll_status"]
    assert db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 2


# -- M2: acquire ------------------------------------------------------------


def test_acquire_stores_feed_body_content(db, monkeypatch):
    _seed_source(db)
    _patch_poll(monkeypatch, _entries(2))
    pipeline.run("manual")

    rows = db.execute("SELECT * FROM content ORDER BY item_id").fetchall()
    assert len(rows) == 2
    assert all(r["method"] == "feed_body" for r in rows)
    assert all(not i["degraded"] for i in db.execute("SELECT degraded FROM items"))


def test_degraded_acquisition_flags_item(db, monkeypatch):
    _seed_source(db)
    entries = _entries(1)
    entries[0]["feed_body_html"] = ""  # force the fetch path

    _patch_poll(monkeypatch, entries)
    monkeypatch.setattr(
        pipeline.web,
        "content",
        lambda item, entry=None: {
            "text": "summary.",
            "method": "summary_fallback",
            "degraded": True,
            "final_url": None,
        },
    )
    pipeline.run("manual")

    assert db.execute("SELECT degraded FROM items").fetchone()["degraded"] == 1
    assert db.execute("SELECT method FROM content").fetchone()["method"] == "summary_fallback"
    html = (config.dist_path() / "dashboard" / "index.html").read_text()
    assert "degraded" in html  # surfaced on the page (plan M2)


def test_acquire_failure_leaves_item_for_retry(db, monkeypatch):
    _seed_source(db)
    _patch_poll(monkeypatch, _entries(1))

    def boom(item, entry=None):
        raise ConnectionError("flaky")

    monkeypatch.setattr(pipeline.web, "content", boom)
    pipeline.run("manual")
    assert db.execute("SELECT COUNT(*) AS n FROM content").fetchone()["n"] == 0
    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "ok"

    # next run: no new entries, but the pending item is retried
    _patch_poll(monkeypatch, [])
    monkeypatch.setattr(
        pipeline.web,
        "content",
        lambda item, entry=None: {
            "text": "recovered text " * 30,
            "method": "extracted",
            "degraded": False,
            "final_url": None,
        },
    )
    pipeline.run("manual")
    assert db.execute("SELECT COUNT(*) AS n FROM content").fetchone()["n"] == 1


# -- M4/M5: gate wiring -------------------------------------------------------


def _patch_gate(monkeypatch, scores: dict[str, int]):
    """fake the messages api: triage scores by title lookup, extraction
    returns a fixed shape. dispatch on the system prompt text."""

    def fake_post_json(url, payload, headers=None, timeout=None):
        system = payload["system"][0]["text"]
        user = payload["messages"][0]["content"]
        if "you are the triage gate" in system:
            score = next((s for title, s in scores.items() if title in user), 0)
            body = json.dumps({"score": score, "justification": "because", "claims": []})
        else:
            body = json.dumps(
                {
                    "bluf": "the bottom line.",
                    "findings": [{"text": "a finding, 42mm", "locator": None}],
                    "specifics": ["42mm"],
                    "not_answered": "",
                }
            )
        return {
            "content": [{"type": "text", "text": body}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(judge.base, "post_json", fake_post_json)


def test_gate_judges_and_extracts_survivors(db, monkeypatch):
    _seed_source(db, threshold=5)
    _patch_poll(monkeypatch, _entries(2))  # "item 0" and "item 1"
    _patch_gate(monkeypatch, {"item 0": 8, "item 1": 2})
    store.set_setting(db, "anthropic_api_key", "k")

    pipeline.run("manual")

    run_row = db.execute("SELECT * FROM runs").fetchone()
    assert (run_row["items_judged"], run_row["items_passed"]) == (2, 1)
    assert db.execute("SELECT COUNT(*) AS n FROM extractions").fetchone()["n"] == 1

    # shadow mode (default): the rejected item still renders, with its reason.
    # there is no shadow-mode banner -- the reason lines are the signal.
    html = (config.dist_path() / "dashboard" / "index.html").read_text()
    assert "the bottom line." in html
    assert "rejected: because" in html
    assert "shadow mode" not in html


def test_gate_live_suppresses_rejections(db, monkeypatch):
    _seed_source(db, threshold=5)
    _patch_poll(monkeypatch, _entries(2))
    _patch_gate(monkeypatch, {"item 0": 8, "item 1": 2})
    store.set_setting(db, "anthropic_api_key", "k")
    store.set_setting(db, "shadow_mode", "false")

    pipeline.run("manual")

    html = (config.dist_path() / "dashboard" / "index.html").read_text()
    assert "item 0" in html
    assert "item 1" not in html  # gate live: rejections invisible


def test_gate_skipped_without_api_key(db, monkeypatch):
    _seed_source(db)
    _patch_poll(monkeypatch, _entries(1))
    pipeline.run("manual")  # no key configured
    assert db.execute("SELECT COUNT(*) AS n FROM judgments").fetchone()["n"] == 0
    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "ok"


def test_reprocess_rejudges_without_polling(db, monkeypatch):
    _seed_source(db, threshold=5)
    _patch_poll(monkeypatch, _entries(1))
    _patch_gate(monkeypatch, {"item 0": 2})
    store.set_setting(db, "anthropic_api_key", "k")
    pipeline.run("manual")

    def no_poll(src):
        raise AssertionError("reprocess must not poll")

    monkeypatch.setattr(pipeline.web, "poll", no_poll)
    _patch_gate(monkeypatch, {"item 0": 9})  # criteria changed, say
    pipeline.reprocess()

    scores = [r["score"] for r in db.execute("SELECT score FROM judgments ORDER BY id")]
    assert scores == [2, 9]  # both judgments kept: shadow comparisons need history


def test_budget_abort_recorded_and_page_still_written(db, monkeypatch):
    _seed_source(db, threshold=5)
    _patch_poll(monkeypatch, _entries(3))
    _patch_gate(monkeypatch, {"item": 8})
    store.set_setting(db, "anthropic_api_key", "k")
    store.set_setting(db, "run_token_budget", "12")  # first call exhausts it

    pipeline.run("manual")

    assert db.execute("SELECT status FROM runs").fetchone()["status"] == "aborted_budget"
    assert (config.dist_path() / "dashboard" / "index.html").exists()


# -- rendering, digest, locators ---------------------------------------------


def test_page_lists_new_item_titles_grouped_by_source(db, monkeypatch):
    _seed_source(db, name="Example Feed")
    _patch_poll(monkeypatch, _entries(2))
    pipeline.run("manual")

    html = (config.dist_path() / "dashboard" / "index.html").read_text()
    assert "Example Feed" in html
    assert "item 1" in html and "item 0" in html
    assert 'href="https://example.com/item-1"' in html


def test_quiet_day_digest_is_nstr_and_push_skipped(db, capsys):
    pipeline.run("manual")
    out = capsys.readouterr().out
    assert '"skipped: digest is NSTR (quiet day)"' in out


def test_digest_contains_bluf_and_finding_links(db, monkeypatch):
    _seed_source(db, threshold=5)
    _patch_poll(monkeypatch, _entries(1))
    _patch_gate(monkeypatch, {"item 0": 8})
    store.set_setting(db, "anthropic_api_key", "k")
    pipeline.run("manual")

    sections = pipeline._sections_for_today(db, datetime.now(store.local_tz(db)))
    digest = pipeline._digest(db, sections)
    assert "the bottom line." in digest
    assert "a finding, 42mm" in digest
    assert "<a href=" in digest


def test_reprocess_rejection_does_not_keep_stale_extraction(db, monkeypatch):
    """regression: the extraction lookup was not scoped to the judgment's run,
    so an item that passed in one run and was rejected on reprocess rendered
    the old body *and* a 'rejected:' line on the same card."""
    _seed_source(db, threshold=5)
    _patch_poll(monkeypatch, _entries(1))
    _patch_gate(monkeypatch, {"item 0": 8})
    store.set_setting(db, "anthropic_api_key", "k")
    pipeline.run("manual")

    sections = pipeline._sections_for_today(db, datetime.now(store.local_tz(db)))
    assert sections[0]["entries"][0]["bluf"]  # passed, so a body is expected

    # re-score the same cached content below the threshold
    _patch_gate(monkeypatch, {"item 0": 2})
    pipeline.reprocess()

    entry = pipeline._sections_for_today(db, datetime.now(store.local_tz(db)))[0]["entries"][0]
    assert entry["passed"] is False
    assert entry["score"] == 2
    assert entry["bluf"] is None, "stale extraction from the earlier run leaked through"
    assert entry["findings"] == []


def _unfetched_item(db, first_seen: datetime):
    """an item that was polled but whose content was never acquired."""
    db.execute(
        "INSERT INTO items (source_id, external_id, canonical_url, title, normalized_title,"
        " published_at, first_seen_at) VALUES (1, 'x', 'https://e/x', 't', 't', ?, ?)",
        (first_seen.isoformat(timespec="seconds"), first_seen.isoformat(timespec="seconds")),
    )
    db.commit()


def test_unfetched_item_inside_retry_window_is_omitted(db):
    """a transient fetch failure is not information -- the item simply appears
    on the day it lands, rather than cluttering today's briefing."""
    _seed_source(db, threshold=5)
    now = datetime.now(UTC)
    _unfetched_item(db, now - timedelta(hours=2))

    sections = pipeline._sections_for_today(db, now)
    assert sections == []


def test_unfetched_item_surfaces_once_retry_window_expires(db):
    """once retries are exhausted the failure is real and must be visible."""
    _seed_source(db, threshold=5)
    now = datetime.now(UTC)
    _unfetched_item(db, now - timedelta(days=pipeline.ACQUIRE_RETRY_DAYS, hours=6))

    entry = pipeline._sections_for_today(db, now)[0]["entries"][0]
    assert entry["unavailable"] is True
    assert entry["score"] is None


def test_late_fetched_item_is_judged_and_rendered(db, monkeypatch):
    """regression: the gate was bounded to today while acquire retried for 3
    days, so an item fetched a day late was cached and then never judged --
    and the page keyed off first_seen, so it could never appear either."""
    _seed_source(db, threshold=5)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    _unfetched_item(db, yesterday)
    db.execute(
        "INSERT INTO content (item_id, method, text, fetched_at) VALUES (1, 'feed_body', ?, ?)",
        ("substantive words. " * 40, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    db.commit()
    _patch_gate(monkeypatch, {"t": 8})
    store.set_setting(db, "anthropic_api_key", "k")

    pipeline.run("manual")

    judged = db.execute("SELECT COUNT(*) AS n FROM judgments WHERE item_id = 1").fetchone()["n"]
    assert judged == 1, "item fetched a day late was never judged"
    titles = [
        e["title"]
        for s in pipeline._sections_for_today(db, datetime.now(UTC))
        for e in s["entries"]
    ]
    assert "t" in titles, "item judged today did not reach today's page"


def test_locator_urls():
    assert (
        pipeline._locator_url("youtube", "https://www.youtube.com/watch?v=abc", "412s")
        == "https://www.youtube.com/watch?v=abc&t=412s"
    )
    assert (
        pipeline._locator_url("web", "https://e/a", "#Thermal results")
        == "https://e/a#thermal-results"
    )
    assert pipeline._locator_url("web", "https://e/a", None) == "https://e/a"
    assert pipeline._locator_url("youtube", "https://e/a", "garbage") == "https://e/a"
