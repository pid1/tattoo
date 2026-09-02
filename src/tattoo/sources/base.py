"""shared http and result envelopes for every outbound call.

reveille's fetchers/base.py, ported: every http call goes through
get_json / get_bytes / get_text / post_json / post_form -- not raw urllib --
so headers and error normalization stay in one place. http and url errors
are normalized to RuntimeError with a greppable message shape
("HTTP {code} from {url}: ..."), which callers may string-match on.

retry/backoff and conditional-request support land with the web adapter
(M1) -- reveille's docstring claimed retries lived in base but the code
made exactly one attempt, so this is new work, not an inherited seam.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

DEFAULT_TIMEOUT = 30.0
USER_AGENT = "tattoo (github.com/pid1/tattoo)"

# retryable http statuses: rate limiting and transient server errors
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER_S = 300.0


# -- envelopes ---------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ok(data: Any) -> dict:
    return {"status": "ok", "data": data, "error": None, "fetched_at": _now_iso()}


def unavailable(error: str) -> dict:
    return {
        "status": "unavailable",
        "data": None,
        "error": error,
        "fetched_at": _now_iso(),
    }


def safe(fn: Callable[[], Any]) -> dict:
    """call fn and wrap the result in an envelope; a failure is data, not an
    exception. pass-through-aware: if fn already produced an envelope it is
    returned untouched."""
    try:
        result = fn()
    except Exception as e:
        return unavailable(f"{type(e).__name__}: {e}")
    if isinstance(result, dict) and result.get("status") in {"ok", "unavailable"}:
        return result
    return ok(result)


# -- http --------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        # Fetching a configured source feed is what this function is for; the
        # existing noqa records the same assessment for ruff.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.HTTPError as e:
        try:
            excerpt = e.read(500).decode("utf-8", errors="replace")
        except Exception:
            excerpt = ""
        raise RuntimeError(f"HTTP {e.code} from {url}: {excerpt}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error fetching {url}: {e.reason}") from e


@dataclass
class FetchResult:
    """result of a conditional GET. status is 200 or 304; on 304 the body is
    empty and the caller's validators are echoed back so they can be
    re-stored unchanged. final_url is the post-redirect url, which is where
    canonical urls come from (plan §3) -- free here, costly anywhere else."""

    status: int
    body: bytes
    etag: str | None
    last_modified: str | None
    final_url: str | None = None


def fetch(
    url: str,
    *,
    headers: dict | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
) -> FetchResult:
    """conditional GET with bounded exponential backoff and jitter.

    new work, not inherited: reveille's base claimed retries but made one
    attempt (plan §0.8). retries fire on 429 and transient 5xx (honoring
    Retry-After, capped) and on network errors; everything else raises the
    normalized RuntimeError immediately."""
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    if etag:
        h["If-None-Match"] = etag
    if last_modified:
        h["If-Modified-Since"] = last_modified

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers=h, method="GET")
        try:
            # The retry path fetches the same configured feed URL as above.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return FetchResult(
                    status=200,
                    body=resp.read(),
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                    final_url=resp.geturl(),
                )
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return FetchResult(
                    status=304,
                    body=b"",
                    etag=etag,
                    last_modified=last_modified,
                    final_url=url,
                )
            retryable = e.code in _RETRYABLE_CODES and attempt < max_attempts
            if not retryable:
                try:
                    excerpt = e.read(500).decode("utf-8", errors="replace")
                except Exception:
                    excerpt = ""
                raise RuntimeError(f"HTTP {e.code} from {url}: {excerpt}") from e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            _sleep_backoff(attempt, backoff_base, retry_after)
        except urllib.error.URLError as e:
            if attempt >= max_attempts:
                raise RuntimeError(f"network error fetching {url}: {e.reason}") from e
            _sleep_backoff(attempt, backoff_base, None)


def _sleep_backoff(attempt: int, base: float, retry_after: str | None) -> None:
    # full jitter in [0.5x, 1.5x] so a fleet of sources never thunders in step
    delay = base * (2 ** (attempt - 1)) * (0.5 + random.random())
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER_S))
        except ValueError:
            pass  # http-date form of Retry-After: rare enough to ignore
    time.sleep(delay)


def get_bytes(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    return _request("GET", url, headers=headers, timeout=timeout)


def get_text(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> str:
    return get_bytes(url, headers=headers, timeout=timeout).decode("utf-8", errors="replace")


def get_json(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return json.loads(get_bytes(url, headers=headers, timeout=timeout).decode("utf-8"))


def post_json(
    url: str,
    payload: dict,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(payload).encode("utf-8")
    return json.loads(_request("POST", url, headers=h, body=body, timeout=timeout).decode("utf-8"))


# -- html/text utilities (reveille port) --------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def strip_html(s: str) -> str:
    """strip html tags, decode entities, normalize whitespace. returns the
    input unchanged if the parser chokes -- degraded text beats no text."""
    try:
        parser = _TextExtractor()
        parser.feed(s)
        parser.close()
        return " ".join(parser.get_text().split())
    except Exception:
        return s


def post_form(
    url: str,
    payload: dict,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """form-encoded post for apis like pushover that don't accept json bodies.
    None values are dropped rather than sent as the string 'None'."""
    import urllib.parse

    h = {"content-type": "application/x-www-form-urlencoded"}
    if headers:
        h.update(headers)
    body = urllib.parse.urlencode({k: v for k, v in payload.items() if v is not None}).encode(
        "utf-8"
    )
    return json.loads(_request("POST", url, headers=h, body=body, timeout=timeout).decode("utf-8"))
