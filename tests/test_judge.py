"""gate contracts (plan §5, §0.5): prompt versioning semantics, strict-json
parsing with the prose fallback, token accounting, and model limits (every
call asks for the model's own maximum output -- there are no token limits)."""

import json

import pytest

from tattoo import judge, store


def _fake_response(text, input_tokens=100, output_tokens=50):
    return {
        "content": [
            {"type": "thinking", "thinking": "hmm"},  # must be filtered out
            {"type": "text", "text": text},
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _patch_llm(monkeypatch, responses):
    """responses: list of raw text bodies returned in order."""
    calls = []

    def fake_post_json(url, payload, headers=None, timeout=None):
        calls.append(payload)
        return _fake_response(responses[min(len(calls) - 1, len(responses) - 1)])

    monkeypatch.setattr(judge.base, "post_json", fake_post_json)
    return calls


def _seed(db, threshold=5):
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO sources (type, feed_url, display_name, threshold, daily_item_cap,"
        " created_at, updated_at) VALUES ('web', 'https://e/f', 'src', ?, 10, ?, ?)",
        (threshold, now, now),
    )
    db.execute(
        "INSERT INTO items (source_id, external_id, canonical_url, title, normalized_title,"
        " first_seen_at) VALUES (1, 'x1', 'https://e/a', 'a title', 'a title', ?)",
        (now,),
    )
    db.execute(
        "INSERT INTO content (item_id, text, method, fetched_at)"
        " VALUES (1, 'the content text', 'extracted', ?)",
        (now,),
    )
    db.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now,))
    db.commit()
    store.set_setting(db, "anthropic_api_key", "test-key")
    judge.ensure_prompts(db)
    return (
        db.execute("SELECT * FROM items WHERE id = 1").fetchone(),
        db.execute("SELECT * FROM content WHERE item_id = 1").fetchone(),
        db.execute("SELECT * FROM sources WHERE id = 1").fetchone(),
    )


# -- prompt versioning ------------------------------------------------------


def test_ensure_prompts_seeds_once(db):
    judge.ensure_prompts(db)
    judge.ensure_prompts(db)  # idempotent
    rows = db.execute("SELECT field_name FROM prompt_history").fetchall()
    assert sorted(r["field_name"] for r in rows) == ["extract_system", "triage_system"]
    _, text = judge.current_prompt(db, "triage_system")
    assert "part number" in text  # the seed came from prompts/triage_system.md


def test_save_inserts_and_repoints(db):
    judge.ensure_prompts(db)
    first_id, _ = judge.current_prompt(db, "triage_system")
    new_id = judge.save_prompt(db, "triage_system", "v2 prompt")
    assert new_id != first_id
    assert judge.current_prompt(db, "triage_system") == (new_id, "v2 prompt")


def test_rollback_repoints_without_inserting(db):
    judge.ensure_prompts(db)
    first_id, first_text = judge.current_prompt(db, "triage_system")
    judge.save_prompt(db, "triage_system", "v2")

    judge.rollback_prompt(db, "triage_system", first_id)
    assert judge.current_prompt(db, "triage_system") == (first_id, first_text)
    count = db.execute(
        "SELECT COUNT(*) AS n FROM prompt_history WHERE field_name = 'triage_system'"
    ).fetchone()["n"]
    assert count == 2  # rollback never inserts

    history = judge.prompt_history(db, "triage_system")
    assert history[0]["current"] is False and history[1]["current"] is True


def test_rollback_rejects_wrong_field(db):
    judge.ensure_prompts(db)
    triage_id, _ = judge.current_prompt(db, "triage_system")
    with pytest.raises(ValueError):
        judge.rollback_prompt(db, "extract_system", triage_id)


# -- json parsing -----------------------------------------------------------


def test_extract_json_strict():
    assert judge._extract_json_object('{"score": 7}') == {"score": 7}


def test_extract_json_wrapped_in_prose():
    text = 'Here is my assessment:\n{"score": 3, "justification": "thin"}\nHope that helps!'
    assert judge._extract_json_object(text)["score"] == 3


def test_extract_json_nested_and_strings_with_braces():
    text = 'x {"a": {"b": 1}, "s": "brace } in string"} y'
    assert judge._extract_json_object(text)["a"] == {"b": 1}


def test_extract_json_none_raises():
    with pytest.raises(ValueError):
        judge._extract_json_object("no json here")


def test_extract_json_strips_code_fence():
    text = '```json\n{"score": 6, "justification": "ok"}\n```'
    assert judge._extract_json_object(text, required_keys=("score",))["score"] == 6


def test_truncated_extraction_raises_instead_of_returning_inner_object():
    """regression: the brace scanner used to skip the unbalanced outer object
    and hand back the first findings element, which stored as an empty row."""
    truncated = (
        '{"bluf": "A summary.", "findings": [{"text": "First finding.", '
        '"locator": "120s"}, {"text": "Second finding that got cut off mid'
    )
    with pytest.raises(judge.TruncatedResponse):
        judge._extract_json_object(truncated, required_keys=("bluf", "findings", "specifics"))


def test_required_keys_rejects_inner_object_but_accepts_outer():
    text = '{"findings": [{"text": "x", "locator": "1s"}], "bluf": "b"}'
    parsed = judge._extract_json_object(text, required_keys=("bluf", "findings"))
    assert parsed["bluf"] == "b"


def test_repairs_invalid_backslash_escape():
    """regression: a model wrote doesn\\'t inside a string. \\' is not a legal
    json escape, so the complete object failed json.loads and the item was
    dropped even though nothing was truncated."""
    text = '{"bluf": "The video doesn\\\'t explain it.", "findings": [], "specifics": []}'
    parsed = judge._extract_json_object(text, required_keys=("bluf", "findings", "specifics"))
    assert parsed["bluf"] == "The video doesn't explain it."


def test_escape_repair_preserves_legitimate_escapes():
    raw = '{"bluf": "a\\\\b", "note": "line\\nbreak", "q": "say \\"hi\\"", "findings": []}'
    parsed = judge._extract_json_object(raw, required_keys=("bluf",))
    assert parsed["bluf"] == "a\\b"
    assert parsed["note"] == "line\nbreak"
    assert parsed["q"] == 'say "hi"'


def test_escape_repair_does_not_corrupt_trailing_backslash_pair():
    # "\\" followed by an apostrophe is valid json; the repair must not eat it
    assert judge._repair_json_escapes(r'"a\\" ') == r'"a\\" '
    assert judge._repair_json_escapes(r"doesn\'t") == "doesn't"


def test_malformed_but_closed_is_not_reported_as_truncated():
    text = '{"bluf": "x", "findings": [, ], "specifics": []}'
    with pytest.raises(ValueError) as exc:
        judge._extract_json_object(text, required_keys=("bluf", "findings", "specifics"))
    assert not isinstance(exc.value, judge.TruncatedResponse)


def _patch_models_api(monkeypatch, response=None, error=None):
    """fake GET /v1/models/{id}; the per-process cache starts empty."""
    calls = []

    def fake_get_json(url, headers=None, timeout=None):
        calls.append((url, headers))
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(judge, "_FETCHED_LIMITS", {})
    monkeypatch.setattr(judge.base, "get_json", fake_get_json)
    return calls


def test_model_max_output_known(db, monkeypatch):
    calls = _patch_models_api(monkeypatch, error=AssertionError("known models never hit the api"))
    assert judge.model_max_output(db, "claude-sonnet-5-5") == 128_000
    assert judge.model_max_output(db, "claude-haiku-5-5") == 128_000
    assert judge.model_max_output(db, "claude-haiku-4-5") == 64_000
    assert calls == []


def test_unknown_model_looked_up_on_models_api_and_cached(db, monkeypatch):
    store.set_setting(db, "anthropic_api_key", "test-key")
    calls = _patch_models_api(
        monkeypatch,
        response={"id": "some-future-model", "max_input_tokens": 2_000_000, "max_tokens": 256_000},
    )
    assert judge.model_max_output(db, "some-future-model") == 256_000
    assert judge._model_limits(db, "some-future-model") == (2_000_000, 256_000)
    assert len(calls) == 1  # cached per process
    url, headers = calls[0]
    assert url == "https://api.anthropic.com/v1/models/some-future-model"
    assert headers == {"x-api-key": "test-key", "anthropic-version": judge.ANTHROPIC_VERSION}


def test_unknown_model_lookup_failure_names_the_model(db, monkeypatch):
    store.set_setting(db, "anthropic_api_key", "test-key")
    _patch_models_api(monkeypatch, error=RuntimeError("HTTP 404 from ...: not_found_error"))
    with pytest.raises(RuntimeError, match="some-future-model"):
        judge.model_max_output(db, "some-future-model")
    assert "some-future-model" not in judge._FETCHED_LIMITS  # a failure is not cached


def test_unknown_model_lookup_with_incomplete_response_raises(db, monkeypatch):
    store.set_setting(db, "anthropic_api_key", "test-key")
    _patch_models_api(monkeypatch, response={"id": "some-future-model"})
    with pytest.raises(RuntimeError, match="some-future-model"):
        judge.model_max_output(db, "some-future-model")


def test_model_max_content_chars_fits_the_context_window(db, monkeypatch):
    store.set_setting(db, "anthropic_api_key", "test-key")
    _patch_models_api(
        monkeypatch,
        response={"max_input_tokens": 1_000_000, "max_tokens": 128_000},
    )
    for model in ("claude-sonnet-5-5", "claude-haiku-4-5", "some-future-model"):
        context, max_output = judge._model_limits(db, model)
        chars = judge.model_max_content_chars(db, model)
        # the content plus a full-ceiling response fits inside the window
        assert chars / judge.CONTENT_CHARS_PER_TOKEN + max_output < context
    assert judge.model_max_content_chars(db, "claude-sonnet-5-5") == 2_130_000
    assert judge.model_max_content_chars(db, "claude-haiku-4-5") == 290_000


def test_refusal_is_reported_as_a_refusal(db, monkeypatch):
    store.set_setting(db, "anthropic_api_key", "test-key")

    def fake_post_json(url, payload, headers=None, timeout=None):
        return {
            "content": [],
            "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber"},
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }

    monkeypatch.setattr(judge.base, "post_json", fake_post_json)
    with pytest.raises(RuntimeError, match="declined.*category=cyber"):
        judge.call_llm(db, None, "claude-haiku-5-5", "sys", "user", 1000)


# -- triage ------------------------------------------------------------------


def test_triage_records_judgment_and_tokens(db, monkeypatch):
    item, content_row, source = _seed(db, threshold=5)
    calls = _patch_llm(monkeypatch, ['{"score": 7, "justification": "dense", "claims": ["42mm"]}'])

    verdict = judge.triage_item(db, 1, item, content_row, source)
    assert verdict["passed"] is True

    row = db.execute("SELECT * FROM judgments").fetchone()
    assert (row["score"], row["passed"]) == (7, 1)
    assert row["prompt_history_id"] is not None
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)

    ledger = json.loads(db.execute("SELECT token_usage FROM runs").fetchone()["token_usage"])
    model = store.get_setting(db, "triage_model")
    assert ledger[model] == {"input": 100, "output": 50}

    # static system prompt is cache_controlled; criteria+content in user turn
    payload = calls[0]
    assert payload["max_tokens"] == judge.model_max_output(db, model)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "the content text" in payload["messages"][0]["content"]


def test_triage_below_threshold_fails_gate(db, monkeypatch):
    item, content_row, source = _seed(db, threshold=5)
    _patch_llm(monkeypatch, ['{"score": 2, "justification": "filler", "claims": []}'])
    verdict = judge.triage_item(db, 1, item, content_row, source)
    assert verdict["passed"] is False


def test_usage_ledger_accumulates_without_a_budget(db, monkeypatch):
    """the run ledger is reporting only: spend keeps accumulating and no call
    is ever refused for it."""
    item, content_row, source = _seed(db)
    _patch_llm(monkeypatch, ['{"score": 5, "justification": "x", "claims": []}'])

    for _ in range(3):
        judge.triage_item(db, 1, item, content_row, source)
    ledger = json.loads(db.execute("SELECT token_usage FROM runs").fetchone()["token_usage"])
    model = store.get_setting(db, "triage_model")
    assert ledger[model] == {"input": 300, "output": 150}


def test_missing_api_key_raises(db, monkeypatch):
    item, content_row, source = _seed(db)
    store.set_setting(db, "anthropic_api_key", "")
    with pytest.raises(RuntimeError, match="api key not configured"):
        judge.triage_item(db, 1, item, content_row, source)


# -- extraction ---------------------------------------------------------------


def test_extract_records_findings(db, monkeypatch):
    item, content_row, source = _seed(db)
    _patch_llm(
        monkeypatch,
        [
            json.dumps(
                {
                    "bluf": "the bottom line.",
                    "findings": [
                        {"text": "finding one, 42mm", "locator": "412s"},
                        {"text": "finding two", "locator": None},
                    ],
                    "specifics": ["42mm", "$120"],
                    "not_answered": "long-term durability",
                }
            )
        ],
    )
    extraction_id = judge.extract_item(db, 1, item, content_row, source)

    extraction = db.execute("SELECT * FROM extractions WHERE id = ?", (extraction_id,)).fetchone()
    assert extraction["bluf"] == "the bottom line."
    assert json.loads(extraction["specifics"]) == ["42mm", "$120"]
    findings = db.execute(
        "SELECT * FROM findings WHERE extraction_id = ? ORDER BY ordinal",
        (extraction_id,),
    ).fetchall()
    assert [f["locator"] for f in findings] == ["412s", None]
