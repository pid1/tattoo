"""the usefulness gate (plan §5): two llm passes with different cost
profiles, called over the raw anthropic messages api through the shared
http wrapper (plan §0.3) -- no sdk. static instructions ride in the system
block with ephemeral cache_control; per-item content is the user message.

prompts are versioned in prompt_history with pointer keys in settings
(rally's pattern, plan §0.5): save = insert + repoint, rollback = repoint
only. seed values come from prompts/*.md on first run; the db is
authoritative thereafter. every judgment and extraction records the
prompt_history_id that produced it, so shadow-mode comparisons are always
within-ruleset.

token usage from each response feeds the run ledger -- reporting only.
there is no run budget and no configurable output cap: every call asks for
the model's own maximum output, so a run never aborts or truncates to save
tokens.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import UTC, datetime

from tattoo import config, store
from tattoo.log import log
from tattoo.sources import base

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_API = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

PROMPT_NAMES = ("triage_system", "extract_system")

# every call sends max_tokens = the model's own maximum output. the api
# requires max_tokens on every call, so there is no literal unlimited -- the
# model ceiling is as close as it gets. billing is on tokens actually
# produced, not on the cap, so the ceiling costs nothing until output grows.

# (context window, max output) per model, in tokens (docs, 2026-09). a cap
# above the model's own output limit is a 400, so a model missing here is
# looked up on the models api rather than guessed at (see _model_limits).
MODEL_LIMITS = {
    "claude-fable-5-1": (1_000_000, 128_000),
    "claude-opus-5-5": (1_000_000, 128_000),
    "claude-opus-5": (1_000_000, 128_000),
    "claude-fable-5": (1_000_000, 128_000),
    "claude-opus-4-8": (1_000_000, 128_000),
    "claude-opus-4-7": (1_000_000, 128_000),
    "claude-opus-4-6": (1_000_000, 128_000),
    "claude-sonnet-5-5": (1_000_000, 128_000),
    "claude-sonnet-5": (1_000_000, 128_000),
    "claude-sonnet-4-6": (1_000_000, 128_000),
    "claude-haiku-5-5": (1_000_000, 128_000),
    "claude-haiku-4-5": (200_000, 64_000),
}

# limits fetched from the models api, per process. a model id's limits do
# not change under it, so one lookup per process is enough.
_FETCHED_LIMITS: dict[str, tuple[int, int]] = {}

# content is clipped only as far as the context window forces: the api
# rejects a request whose input plus max_tokens exceeds the window, so the
# full output ceiling is reserved, plus headroom for the system prompt and
# criteria. chars-per-token is deliberately low so dense text (code, urls,
# non-english) still fits.
PROMPT_HEADROOM_TOKENS = 20_000
CONTENT_CHARS_PER_TOKEN = 2.5

GENERIC_CRITERIA = (
    "no source-specific criteria are configured. apply the generic signal "
    "definition from the system prompt as-is."
)


class TruncatedResponse(RuntimeError):
    """the model hit max_tokens mid-json. distinct from a merely malformed
    response because the output is already the model's maximum: the
    request needs to ask for less, not for a bigger ceiling."""


def _api_headers(conn) -> dict:
    api_key = store.get_secret(conn, "anthropic_api_key")
    if not api_key:
        raise RuntimeError("anthropic api key not configured (settings or ANTHROPIC_API_KEY)")
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}


def _model_limits(conn, model: str) -> tuple[int, int]:
    """(context window, max output) for a model: the known table first, then
    the models api, cached per process. a failed lookup raises naming the
    model -- guessing a small output cap would truncate silently instead."""
    model = (model or "").strip()
    limits = MODEL_LIMITS.get(model) or _FETCHED_LIMITS.get(model)
    if limits is not None:
        return limits
    if not model:
        raise RuntimeError("no model configured")
    headers = _api_headers(conn)  # a missing key keeps its own message
    url = f"{ANTHROPIC_MODELS_API}/{urllib.parse.quote(model, safe='')}"
    try:
        resp = base.get_json(url, headers=headers, timeout=30.0)
        limits = (int(resp["max_input_tokens"]), int(resp["max_tokens"]))
    except Exception as e:
        raise RuntimeError(
            f"could not look up limits for model {model!r} on the models api"
            f" ({type(e).__name__}: {e}); add it to judge.MODEL_LIMITS or fix the model id"
        ) from e
    _FETCHED_LIMITS[model] = limits
    log(
        "judge",
        "model limits fetched",
        model=model,
        context=limits[0],
        max_tokens=limits[1],
    )
    return limits


def model_max_output(conn, model: str) -> int:
    return _model_limits(conn, model)[1]


def model_max_content_chars(conn, model: str) -> int:
    """the most content that fits beside a full-ceiling response."""
    context, max_output = _model_limits(conn, model)
    return int((context - max_output - PROMPT_HEADROOM_TOKENS) * CONTENT_CHARS_PER_TOKEN)


# -- prompt versioning (rally's history + pointer pattern) -----------------


def _pointer_key(name: str) -> str:
    return f"current_{name}_history_id"


def ensure_prompts(conn) -> None:
    """seed prompt_history from prompts/*.md the first time; a no-op once
    pointers exist. called at startup."""
    for name in PROMPT_NAMES:
        if store.get_setting(conn, _pointer_key(name)):
            continue
        path = config.REPO_ROOT / "prompts" / f"{name}.md"
        save_prompt(conn, name, path.read_text(encoding="utf-8").strip())
        log("judge", "prompt seeded", prompt=name)


def current_prompt(conn, name: str) -> tuple[int, str]:
    pointer = store.get_setting(conn, _pointer_key(name))
    if pointer:
        row = conn.execute(
            "SELECT id, value FROM prompt_history WHERE id = ?", (int(pointer),)
        ).fetchone()
        if row:
            return row["id"], row["value"]
    raise RuntimeError(f"no current prompt for {name}; ensure_prompts() has not run")


def save_prompt(conn, name: str, value: str) -> int:
    """save = insert a history row and repoint. one timestamp so
    created_at == last_used_at on insert."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO prompt_history (field_name, value, created_at, last_used_at)"
        " VALUES (?, ?, ?, ?)",
        (name, value, now, now),
    )
    history_id = cur.lastrowid
    conn.commit()
    store.set_setting(conn, _pointer_key(name), str(history_id))
    return history_id


def rollback_prompt(conn, name: str, history_id: int) -> None:
    """rollback = repoint and bump last_used_at; never inserts."""
    row = conn.execute(
        "SELECT id FROM prompt_history WHERE id = ? AND field_name = ?",
        (history_id, name),
    ).fetchone()
    if row is None:
        raise ValueError(f"no history row {history_id} for {name}")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute("UPDATE prompt_history SET last_used_at = ? WHERE id = ?", (now, history_id))
    conn.commit()
    store.set_setting(conn, _pointer_key(name), str(history_id))


def prompt_history(conn, name: str) -> list[dict]:
    current_id, _ = current_prompt(conn, name)
    rows = conn.execute(
        "SELECT id, value, created_at, last_used_at FROM prompt_history"
        " WHERE field_name = ? ORDER BY created_at DESC, id DESC",
        (name,),
    ).fetchall()
    return [
        {
            "history_id": r["id"],
            "value": r["value"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "current": r["id"] == current_id,
        }
        for r in rows
    ]


# -- the api call -----------------------------------------------------------


def call_llm(
    conn,
    run_id: int | None,
    model: str,
    system_text: str,
    user_text: str,
    max_tokens: int,
) -> tuple[str, dict]:
    """one messages-api call with usage accounting.
    returns (text, usage) -- text is the concatenated text blocks (thinking
    blocks filtered out), usage feeds per-item token attribution."""
    headers = _api_headers(conn)
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_text}],
    }
    resp = base.post_json(ANTHROPIC_API, payload, headers=headers, timeout=120.0)

    usage = resp.get("usage") or {}
    if run_id is not None:
        _record_usage(conn, run_id, model, usage)

    # a safety-classifier decline is an http 200 with no usable text. name it
    # so the log says why instead of reporting an empty response.
    if resp.get("stop_reason") == "refusal":
        category = (resp.get("stop_details") or {}).get("category")
        raise RuntimeError(f"{model} declined the request (refusal, category={category})")

    # filter by block type: newer models may return thinking blocks first
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    if not text:
        raise RuntimeError(f"empty response from {model}")

    # a max_tokens stop means the json is cut mid-structure. surfacing it here
    # keeps the diagnosis one log line away instead of an empty extraction.
    if resp.get("stop_reason") == "max_tokens":
        raise TruncatedResponse(
            f"{model} hit max_tokens={max_tokens} (its maximum output) mid-response"
        )
    return text, usage


def _usage_tokens(usage: dict) -> tuple[int, int]:
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _record_usage(conn, run_id: int, model: str, usage: dict) -> None:
    input_t, output_t = _usage_tokens(usage)
    row = conn.execute("SELECT token_usage FROM runs WHERE id = ?", (run_id,)).fetchone()
    ledger = json.loads(row["token_usage"] or "{}")
    entry = ledger.setdefault(model, {"input": 0, "output": 0})
    entry["input"] += input_t
    entry["output"] += output_t
    conn.execute("UPDATE runs SET token_usage = ? WHERE id = ?", (json.dumps(ledger), run_id))
    conn.commit()


# -- pass 1: triage ---------------------------------------------------------


def triage_item(conn, run_id: int, item, content_row, source) -> dict:
    """score one item; record the judgment. returns
    {score, justification, passed, judgment_id}."""
    prompt_id, system_text = current_prompt(conn, "triage_system")
    model = store.get_setting(conn, "triage_model")

    user_text = _triage_user_prompt(conn, item, content_row, source, model)
    max_tokens = model_max_output(conn, model)
    raw, usage = call_llm(conn, run_id, model, system_text, user_text, max_tokens)
    parsed = _extract_json_object(raw, required_keys=("score", "justification"))
    input_t, output_t = _usage_tokens(usage)

    score = max(0, min(10, int(parsed.get("score", 0))))
    justification = str(parsed.get("justification", "")).strip()
    passed = score >= source["threshold"]

    now = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO judgments (item_id, run_id, prompt_history_id, score, justification,"
        " passed, created_at, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["id"],
            run_id,
            prompt_id,
            score,
            justification,
            int(passed),
            now,
            input_t,
            output_t,
        ),
    )
    conn.commit()
    return {
        "score": score,
        "justification": justification,
        "passed": passed,
        "judgment_id": cur.lastrowid,
    }


def _triage_user_prompt(conn, item, content_row, source, model: str) -> str:
    criteria = (source["criteria"] or "").strip() or GENERIC_CRITERIA
    degraded = bool(item["degraded"])
    return (
        f"SOURCE: {source['display_name']} ({source['type']})\n"
        f"CRITERIA:\n{criteria}\n"
        f"DEGRADED: {'true' if degraded else 'false'}"
        f" (acquisition: {content_row['method']})\n"
        f"TITLE: {item['title']}\n"
        f"CONTENT:\n{content_row['text'][: model_max_content_chars(conn, model)]}"
    )


# -- pass 2: extraction ------------------------------------------------------


def extract_item(conn, run_id: int, item, content_row, source) -> int:
    """extract bluf/findings/specifics for a passing item. returns the
    extraction id."""
    prompt_id, system_text = current_prompt(conn, "extract_system")
    model = store.get_setting(conn, "extract_model")

    user_text = (
        f"SOURCE: {source['display_name']} ({source['type']})\n"
        f"TITLE: {item['title']}\n"
        f"CONTENT:\n{content_row['text'][: model_max_content_chars(conn, model)]}"
    )
    max_tokens = model_max_output(conn, model)
    raw, usage = call_llm(conn, run_id, model, system_text, user_text, max_tokens)
    parsed = _extract_json_object(raw, required_keys=("bluf", "findings"))
    input_t, output_t = _usage_tokens(usage)

    # an extraction with neither a bluf nor a finding renders as a blank card.
    # fail loudly so the pipeline logs and skips instead of storing the hole.
    if not str(parsed.get("bluf", "")).strip() and not parsed.get("findings"):
        raise ValueError("extraction had no bluf and no findings")

    now = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO extractions (item_id, run_id, prompt_history_id, bluf, not_answered,"
        " specifics, created_at, input_tokens, output_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["id"],
            run_id,
            prompt_id,
            str(parsed.get("bluf", "")).strip(),
            str(parsed.get("not_answered", "")).strip(),
            json.dumps([str(s) for s in parsed.get("specifics", [])]),
            now,
            input_t,
            output_t,
        ),
    )
    extraction_id = cur.lastrowid
    for ordinal, finding in enumerate(parsed.get("findings", [])):
        text = str(finding.get("text", "")).strip()
        if not text:
            continue
        locator = finding.get("locator")
        conn.execute(
            "INSERT INTO findings (extraction_id, ordinal, text, locator) VALUES (?, ?, ?, ?)",
            (extraction_id, ordinal, text, str(locator) if locator else None),
        )
    conn.commit()
    return extraction_id


# -- json parsing ------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """models wrap json in ```json fences often enough to handle directly
    rather than leaving it to the brace scanner."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    body = stripped[3:]
    if body[:4].lower().startswith("json"):
        body = body[4:]
    end = body.rfind("```")
    return body[:end] if end != -1 else body


_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _repair_json_escapes(text: str) -> str:
    """drop backslashes that json does not permit as escapes. models reach
    for python/js habits -- \\' inside a double-quoted string is the common
    one -- which fails json.loads even though the object is complete.

    the regex consumes backslash pairs left to right, so a legitimate \\\\
    is matched as a unit and preserved rather than having its second
    backslash mistaken for the start of a bad escape.
    """
    return re.sub(
        r"\\(.)",
        lambda m: m.group(0) if m.group(1) in _VALID_JSON_ESCAPES else m.group(1),
        text,
        flags=re.S,
    )


def _extract_json_object(text: str, required_keys: tuple[str, ...] = ()) -> dict:
    """strict parse first, then fall back to the first balanced object in
    arbitrary text (rally's pattern) -- models occasionally wrap json in
    prose despite instructions.

    required_keys guards the fallback. without it a truncated response makes
    the scanner skip the unbalanced outer object and return the first inner
    one -- for an extraction, a lone {"text", "locator"} findings element,
    which has none of the fields the caller reads and so lands in the db as
    a silently empty row. demanding a recognisable key turns that into a
    raised error the pipeline logs and skips.
    """
    text = _strip_code_fence(text)

    def acceptable(candidate: object) -> bool:
        if not isinstance(candidate, dict):
            return False
        return not required_keys or any(k in candidate for k in required_keys)

    for candidate_text in (text, _repair_json_escapes(text)):
        try:
            parsed = json.loads(candidate_text)
        except ValueError:
            continue
        if acceptable(parsed):
            return parsed

    start = text.find("{")
    first_candidate_closed = True
    saw_unclosed = False
    while start != -1:
        depth = 0
        closed = False
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        closed = True
                        slice_ = text[start : i + 1]
                        for attempt in (slice_, _repair_json_escapes(slice_)):
                            try:
                                candidate = json.loads(attempt)
                            except ValueError:
                                continue
                            if acceptable(candidate):
                                return candidate
                        break
        if start == text.find("{"):
            first_candidate_closed = closed
        saw_unclosed = saw_unclosed or not closed
        start = text.find("{", start + 1)

    # only the first object failing to close means the response was cut off.
    # a later unclosed brace is just a stray "{" in trailing prose, and a
    # closed-but-unparseable object is malformed rather than truncated.
    if not first_candidate_closed and saw_unclosed:
        raise TruncatedResponse(f"json object never closed (truncated): {text[-200:]!r}")
    raise ValueError(f"no usable json object in model output: {text[:200]!r}")
