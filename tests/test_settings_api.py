"""settings api contract (plan §0.4): secrets never travel back to the
page, blank means keep, env overrides are visible as metadata only."""

from tattoo import store


def test_get_settings_returns_defaults_and_secret_metadata(client):
    data = client.get("/api/settings").json()
    assert data["settings"]["schedule_time"] == "21:00"
    assert data["settings"]["shadow_mode"] == "true"
    assert data["secrets"]["anthropic_api_key"] == {
        "set": False,
        "hint": "",
        "env_override": False,
    }


def test_secret_value_never_returned(client, db):
    store.set_setting(db, "anthropic_api_key", "sk-ant-secret-key-abcd")
    data = client.get("/api/settings").json()
    body = client.get("/api/settings").text
    assert "sk-ant-secret-key-abcd" not in body
    assert data["secrets"]["anthropic_api_key"]["set"] is True
    assert data["secrets"]["anthropic_api_key"]["hint"] == "…abcd"


def test_put_settings_upserts(client, db):
    resp = client.put(
        "/api/settings",
        json={
            "settings": {
                "schedule_time": "06:30",
                "anthropic_api_key": "sk-new-key-wxyz",
            }
        },
    )
    assert resp.status_code == 200
    assert store.get_setting(db, "schedule_time") == "06:30"
    assert store.get_secret(db, "anthropic_api_key") == "sk-new-key-wxyz"


def test_blank_secret_keeps_existing(client, db):
    store.set_setting(db, "pushover_api_key", "original")
    client.put("/api/settings", json={"settings": {"pushover_api_key": ""}})
    assert store.get_secret(db, "pushover_api_key") == "original"


def test_unknown_key_rejected(client):
    resp = client.put("/api/settings", json={"settings": {"evil_key": "x"}})
    assert resp.status_code == 400


def test_env_override_reported(client, db, monkeypatch):
    monkeypatch.setenv("PUSHOVER_API_KEY", "env-token-1234")
    data = client.get("/api/settings").json()
    assert data["secrets"]["pushover_api_key"]["env_override"] is True
    assert data["secrets"]["pushover_api_key"]["hint"] == "…1234"


def test_api_responses_not_cacheable(client):
    resp = client.get("/api/settings")
    assert resp.headers["cache-control"] == "no-store"


def test_settings_page_serves(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "tattoo — settings" in resp.text
    # the write-only convention is visible in the page contract
    assert 'type="password"' in resp.text


# -- prompts api ---------------------------------------------------------


def test_prompt_roundtrip_and_rollback(client):
    first = client.get("/api/prompts/triage_system").json()
    assert "part number" in first["value"]

    saved = client.put("/api/prompts/triage_system", json={"value": "v2 prompt"}).json()
    assert saved["history_id"] != first["history_id"]

    history = client.get("/api/prompts/triage_system/history").json()["history"]
    assert len(history) == 2
    assert history[0]["current"] is True  # newest-first, v2 current

    rolled = client.post(
        "/api/prompts/triage_system/rollback", json={"history_id": first["history_id"]}
    ).json()
    assert rolled["value"] == first["value"]
    # rollback never inserts
    assert len(client.get("/api/prompts/triage_system/history").json()["history"]) == 2


def test_unknown_prompt_404(client):
    assert client.get("/api/prompts/nope").status_code == 404


def test_empty_prompt_rejected(client):
    assert (
        client.put("/api/prompts/triage_system", json={"value": "  "}).status_code
        == 422
    )
