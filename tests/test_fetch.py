"""fetch() retry/conditional contract (plan §0.8): 304 short-circuits,
429/5xx retry with backoff, non-retryable codes raise immediately with the
normalized message shape."""

import io
import urllib.error
import urllib.request

import pytest

from tattoo.sources import base


class _Resp:
    def __init__(self, body=b"ok", headers=None, url="https://example.com"):
        self._body = body
        self.headers = headers or {}
        self._url = url

    def read(self):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://example.com", code, "err", headers or {}, io.BytesIO(b"body")
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(base.time, "sleep", sleeps.append)
    return sleeps


def test_fetch_304_echoes_validators(monkeypatch):
    def fake_urlopen(req, timeout=None):
        assert req.get_header("If-none-match") == 'W/"e1"'
        raise _http_error(304)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = base.fetch("https://example.com/feed", etag='W/"e1"', last_modified="lm")
    assert result.status == 304
    assert result.body == b""
    assert (result.etag, result.last_modified) == ('W/"e1"', "lm")


def test_fetch_retries_429_then_succeeds(monkeypatch, no_sleep):
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise _http_error(429, headers={"Retry-After": "7"})
        return _Resp(b"payload", headers={"ETag": 'W/"e2"'})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = base.fetch("https://example.com/feed")
    assert result.status == 200
    assert result.body == b"payload"
    assert len(attempts) == 2
    assert len(no_sleep) == 1
    assert no_sleep[0] >= 7  # Retry-After honored as a floor


def test_fetch_gives_up_after_max_attempts(monkeypatch, no_sleep):
    def fake_urlopen(req, timeout=None):
        raise _http_error(503)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 503 from"):
        base.fetch("https://example.com/feed", max_attempts=3)
    assert len(no_sleep) == 2  # slept between attempts, not after the last


def test_fetch_non_retryable_raises_immediately(monkeypatch, no_sleep):
    def fake_urlopen(req, timeout=None):
        raise _http_error(404)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 404 from"):
        base.fetch("https://example.com/feed")
    assert no_sleep == []


def test_fetch_network_error_retries(monkeypatch, no_sleep):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="network error fetching"):
        base.fetch("https://example.com/feed", max_attempts=2)
    assert len(no_sleep) == 1
