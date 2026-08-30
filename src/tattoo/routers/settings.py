"""settings + prompt apis (plan §8). rally's route conventions; NOT rally's
secret handling -- secrets never travel back to the page (plan §0.4).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from tattoo import judge, notifier, store
from tattoo.database import get_db

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
def get_settings(db: sqlite3.Connection = Depends(get_db)) -> dict:
    return store.settings_surface(db)


@router.put("/api/settings")
def put_settings(body: dict, db: sqlite3.Connection = Depends(get_db)) -> dict:
    updates = body.get("settings")
    if not isinstance(updates, dict):
        raise HTTPException(status_code=422, detail="body must be {'settings': {key: value}}")
    try:
        applied = store.apply_settings(db, updates)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"unknown settings: {e.args[0]}") from e
    surface = store.settings_surface(db)
    surface["applied"] = applied
    return surface


@router.post("/api/settings/test-llm")
def test_llm(db: sqlite3.Connection = Depends(get_db)) -> dict:
    """rally's verify-on-save shape: {success, message|error}. a real
    1-token call against the configured key and triage model."""
    model = store.get_setting(db, "triage_model")
    try:
        judge.call_llm(db, None, model, "reply with the single word: ok", "hi", 8)
        return {"success": True, "message": f"connected to {model}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


@router.post("/api/settings/test-pushover")
def test_pushover(db: sqlite3.Connection = Depends(get_db)) -> dict:

    token = store.get_secret(db, "pushover_api_key")
    user_key = store.get_secret(db, "pushover_user_key")
    if not token or not user_key:
        return {"success": False, "error": "pushover keys not configured"}
    try:
        resp = notifier.post_form(
            notifier.PUSHOVER_URL,
            {
                "token": token,
                "user": user_key,
                "title": "tattoo test",
                "message": "test notification from the settings page.",
                "priority": "-1",
            },
            timeout=15.0,
        )
        if isinstance(resp, dict) and resp.get("status") == 1:
            return {"success": True, "message": "test notification sent"}
        return {"success": False, "error": str(resp.get("errors") or resp)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# -- prompts (history + pointer pattern, plan §0.5) ---------------------------


def _validate_prompt_name(name: str) -> str:
    if name not in judge.PROMPT_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown prompt {name}")
    return name


@router.get("/api/prompts/{name}")
def get_prompt(name: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _validate_prompt_name(name)
    judge.ensure_prompts(db)
    history_id, value = judge.current_prompt(db, name)
    return {"name": name, "value": value, "history_id": history_id}


@router.put("/api/prompts/{name}")
def put_prompt(name: str, body: dict, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _validate_prompt_name(name)
    judge.ensure_prompts(db)
    value = (body.get("value") or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="prompt value must not be empty")
    history_id = judge.save_prompt(db, name, value)
    return {"name": name, "value": value, "history_id": history_id}


@router.get("/api/prompts/{name}/history")
def get_prompt_history(name: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _validate_prompt_name(name)
    judge.ensure_prompts(db)
    return {"name": name, "history": judge.prompt_history(db, name)}


@router.post("/api/prompts/{name}/rollback")
def rollback_prompt(name: str, body: dict, db: sqlite3.Connection = Depends(get_db)) -> dict:
    _validate_prompt_name(name)
    judge.ensure_prompts(db)
    try:
        history_id = int(body["history_id"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail="body must be {'history_id': int}") from e
    try:
        judge.rollback_prompt(db, name, history_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    history_id, value = judge.current_prompt(db, name)
    return {"name": name, "value": value, "history_id": history_id}
