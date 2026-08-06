"""notifier contract (reveille's): quiet days skip, missing keys skip with
a logged reason, truncation beats a 4xx, and send_pushover never raises."""

import json
from datetime import UTC, datetime

from tattoo import notifier

NOW = datetime(2026, 8, 6, 21, 0, tzinfo=UTC)


# -- _should_send --------------------------------------------------------


def test_should_send_none():
    assert notifier._should_send(None) == (False, "no digest")


def test_should_send_empty():
    ok, reason = notifier._should_send("   ")
    assert not ok and "empty" in reason


def test_should_send_nstr_variants():
    for text in ("NSTR", "NSTR.", "nstr.", "  nstr  "):
        ok, reason = notifier._should_send(text)
        assert not ok and "NSTR" in reason


def test_should_send_real_content():
    assert notifier._should_send("three findings from S2 Underground.")[0]


def test_nstr_embedded_in_longer_text_still_sends():
    assert notifier._should_send("NSTR for feeds, but one alert passed.")[0]


# -- truncation ----------------------------------------------------------


def test_truncate_short_text_unchanged():
    assert notifier._truncate_message("hello") == "hello"


def test_truncate_long_text_at_limit():
    out = notifier._truncate_message("word " * 400)
    assert len(out) <= notifier._MAX_MESSAGE_CHARS
    assert out.endswith(" ...")


# -- send_pushover -------------------------------------------------------


def _sent_payloads(monkeypatch):
    calls = []

    def fake_post_form(url, payload, headers=None, timeout=None):
        calls.append(payload)
        return {"status": 1}

    monkeypatch.setattr(notifier, "post_form", fake_post_form)
    return calls


def test_send_skips_without_keys(monkeypatch, capsys):
    calls = _sent_payloads(monkeypatch)
    notifier.send_pushover("real digest", NOW, token=None, user_key=None)
    assert calls == []
    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "not configured" in logged["msg"]


def test_send_builds_payload(monkeypatch):
    calls = _sent_payloads(monkeypatch)
    notifier.send_pushover(
        "digest body", NOW, token="t", user_key="u", page_url="http://nas/dashboard/?d=2026-08-06"
    )
    assert len(calls) == 1
    payload = calls[0]
    assert payload["message"] == "digest body"
    assert payload["title"] == "tattoo 2026-08-06"
    assert payload["url"] == "http://nas/dashboard/?d=2026-08-06"
    assert payload["url_title"] == "full briefing"


def test_send_never_raises(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("HTTP 500 from pushover")

    monkeypatch.setattr(notifier, "post_form", boom)
    notifier.send_pushover("digest", NOW, token="t", user_key="u")  # must not raise
    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert logged["level"] == "error"


def test_api_rejection_logged_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(
        notifier, "post_form", lambda *a, **k: {"status": 0, "errors": ["user key invalid"]}
    )
    notifier.send_pushover("digest", NOW, token="t", user_key="u")
    logged = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert logged["level"] == "warn"


def test_send_failure_uses_lowest_priority(monkeypatch):
    calls = _sent_payloads(monkeypatch)
    notifier.send_failure("run failed: boom", NOW, token="t", user_key="u")
    assert calls[0]["priority"] == "-2"
    assert "FAILED" in calls[0]["title"]
