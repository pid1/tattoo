"""pushover notification (reveille's notifier.py, ported).

runs after the page set is written. never raises: pushover being down or
misconfigured must not prevent the pages from being published. every skip
path logs a reason so silence in the log is never ambiguous.

the message body carries real content, not a teaser (plan §7): the
dashboard link is dead whenever tailscale is down on the phone, so the
digest must stand alone. quiet days skip entirely -- the NSTR contract.
"""

from __future__ import annotations

from datetime import datetime

from tattoo.log import log
from tattoo.sources.base import post_form

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_MAX_MESSAGE_CHARS = 1024  # pushover limit, per their api docs
_MAX_TITLE_CHARS = 250


def _should_send(digest: str | None) -> tuple[bool, str]:
    if digest is None:
        return False, "no digest"
    text = digest.strip()
    if not text:
        return False, "digest is empty"
    # tolerant NSTR detection: with or without trailing period, any case
    bare = text.rstrip(".").strip().upper()
    if bare == "NSTR":
        return False, "digest is NSTR (quiet day)"
    return True, ""


def _truncate_message(text: str) -> str:
    """cut at the pushover limit rather than getting rejected with a 4xx.
    back up to a word boundary when one is close."""
    if len(text) <= _MAX_MESSAGE_CHARS:
        return text
    cut = _MAX_MESSAGE_CHARS - 4
    space = text.rfind(" ", 0, cut)
    if space >= cut - 100:
        cut = space
    return text[:cut].rstrip() + " ..."


def send_pushover(
    digest: str | None,
    now: datetime,
    *,
    token: str | None,
    user_key: str | None,
    page_url: str | None = None,
    title: str | None = None,
    priority: int | None = None,
    html: bool = False,
) -> None:
    """never raises. logs the outcome either way."""
    try:
        should, reason = _should_send(digest)
        if not should:
            log("pushover", f"skipped: {reason}")
            return
        if not token or not user_key:
            log("pushover", "skipped: pushover keys not configured")
            return

        payload = {
            "token": token,
            "user": user_key,
            "title": (title or f"tattoo {now.strftime('%Y-%m-%d')}")[:_MAX_TITLE_CHARS],
            "message": _truncate_message(digest.strip()),
        }
        if html:
            payload["html"] = "1"
        if priority is not None:
            payload["priority"] = str(priority)
        if page_url:
            payload["url"] = page_url
            payload["url_title"] = "full briefing"

        resp = post_form(PUSHOVER_URL, payload, timeout=15.0)
        if isinstance(resp, dict) and resp.get("status") == 1:
            log("pushover", "sent ok")
        else:
            errors = resp.get("errors") if isinstance(resp, dict) else resp
            log("pushover", "api rejected", level="warn", errors=errors or resp)
    except Exception as e:
        log("pushover", f"send failed: {type(e).__name__}: {e}", level="error")


def send_failure(
    message: str, now: datetime, *, token: str | None, user_key: str | None
) -> None:
    """failure notification, lowest priority (-2): no sound, no vibration,
    just a badge (plan §9)."""
    send_pushover(
        message,
        now,
        token=token,
        user_key=user_key,
        title=f"tattoo FAILED {now.strftime('%Y-%m-%d')}",
        priority=-2,
    )
